from __future__ import annotations

import sys

import pytest

from .accounts import (
    MigrationRefused,
    change_owner_password,
    ensure_owner,
    password_hash_of,
    area_config,
    config_for_user,
    create_user,
    delete_user,
    get_user,
    migrate_to_accounts,
    new_user_id,
    owner_config,
    user_path,
)
from .config import Config
from .db import connect, migrate

VALID_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def cfg(tmp_path):
    return Config(root=tmp_path)


def area(cfg, user_id=VALID_ID):
    path = user_path(cfg, user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------- user_path ----------


def test_accepts_a_path_inside_the_area(cfg):
    area(cfg)
    resolved = user_path(cfg, VALID_ID, "library", "serie", "001", "p0001.jpg")

    assert resolved is not None
    assert resolved.name == "p0001.jpg"
    assert user_path(cfg, VALID_ID) in resolved.parents


def test_rejects_dot_dot(cfg):
    area(cfg)
    assert user_path(cfg, VALID_ID, "..") is None
    assert user_path(cfg, VALID_ID, "library", "..", "..", ".env") is None


def test_treats_percent_encoded_dot_dot_as_a_literal_name(cfg):
    """`%2e%2e` so vira `..` depois de decodificado, e quem decodifica e o HTTP.

    Chegando aqui sem decodificar, e um nome de pasta esquisito que nao sai da
    area - e recusa-lo mentiria sobre o que esta funcao faz. A tranca contra a
    forma codificada e o `unquote` antes do `safe_component`, no painel; esta
    linha existe para o dia em que alguem trocar a ordem das duas.
    """
    area(cfg)
    assert user_path(cfg, VALID_ID, "%2e%2e") is not None
    assert user_path(cfg, VALID_ID, "..") is None


def test_rejects_an_absolute_path(cfg, tmp_path):
    area(cfg)
    outside = tmp_path / "fora.txt"
    outside.write_text("segredo", encoding="utf-8")

    assert user_path(cfg, VALID_ID, str(outside)) is None
    assert user_path(cfg, VALID_ID, "library", str(outside)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlink no Windows exige privilegio")
def test_rejects_a_symlink_leaving_the_area(cfg, tmp_path):
    inside = area(cfg)
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "segredo.txt").write_text("segredo", encoding="utf-8")
    (inside / "atalho").symlink_to(outside, target_is_directory=True)

    assert user_path(cfg, VALID_ID, "atalho", "segredo.txt") is None


def test_rejects_an_id_that_is_not_an_id(cfg):
    for invented in ("", "..", "dono", "../../etc", VALID_ID.upper(), VALID_ID + "a", "0" * 31):
        assert user_path(cfg, invented, "library") is None, invented


def test_rejects_an_id_with_a_separator(cfg):
    assert user_path(cfg, f"{VALID_ID}/..") is None
    assert user_path(cfg, f"..\\{VALID_ID}") is None


# ---------- config derivado ----------


def test_points_library_and_output_at_the_user_area(cfg):
    derived = config_for_user(cfg, VALID_ID)

    assert derived.library_dir == user_path(cfg, VALID_ID) / "library"
    assert derived.output_dir == user_path(cfg, VALID_ID) / "output"


def test_keeps_the_database_out_of_the_user_area(cfg):
    """O banco e de todos. Derivado por usuario, cada um teria o seu."""
    derived = config_for_user(cfg, VALID_ID)

    assert derived.data_dir == cfg.data_dir


def test_keeps_everything_else_from_the_original_config(cfg):
    derived = config_for_user(cfg, VALID_ID)

    assert derived.ocr == cfg.ocr
    assert derived.detect == cfg.detect
    assert derived.translation == cfg.translation


def test_refuses_to_derive_for_an_invented_id(cfg):
    assert config_for_user(cfg, "dono") is None


# ---------- registros ----------


def test_stores_and_reads_back_a_user(cfg):
    migrate(cfg)
    with connect(cfg) as connection:
        created = create_user(connection, kind="owner", email="  Dono@Example.COM ")
        found = get_user(connection, created.id)

    assert found == created
    assert found.email == "dono@example.com"
    assert found.is_owner


def test_removes_the_area_from_disk_when_the_user_goes(cfg):
    migrate(cfg)
    with connect(cfg) as connection:
        user = create_user(connection, kind="tester", user_id=new_user_id())
        page = user_path(cfg, user.id, "library", "serie", "001", "p0001.jpg")
        page.parent.mkdir(parents=True)
        page.write_bytes(b"x" * 100)

        freed = delete_user(cfg, connection, user.id)

        assert freed == 100
        assert get_user(connection, user.id) is None
        assert not user_path(cfg, user.id).exists()


# ---------- migracao do acervo ----------


def _seed_project(root):
    chapter = root / "library" / "serie" / "001"
    chapter.mkdir(parents=True)
    (chapter / "p0001.jpg").write_bytes(b"\xff\xd8\xffpagina")
    (root / "output" / "serie" / "001").mkdir(parents=True)
    (root / "output" / "library.json").write_text("{}", encoding="utf-8")


def test_moves_the_existing_library_into_the_owner_area(cfg):
    _seed_project(cfg.root)

    result = migrate_to_accounts(cfg)

    assert set(result.moved) == {"library", "output"}
    assert not (cfg.root / "library").exists()
    assert user_path(cfg, result.owner_id, "library", "serie", "001", "p0001.jpg").is_file()
    assert user_path(cfg, result.owner_id, "output", "library.json").is_file()


def test_migrating_twice_moves_nothing_and_keeps_the_same_owner(cfg):
    _seed_project(cfg.root)

    first = migrate_to_accounts(cfg)
    second = migrate_to_accounts(cfg)

    assert second.owner_id == first.owner_id
    assert second.moved == ()
    assert user_path(cfg, first.owner_id, "library", "serie", "001", "p0001.jpg").is_file()


def test_refuses_when_another_user_already_has_an_area(cfg):
    _seed_project(cfg.root)
    stranger = user_path(cfg, new_user_id())
    stranger.mkdir(parents=True)

    with pytest.raises(MigrationRefused):
        migrate_to_accounts(cfg)

    assert (cfg.root / "library").is_dir()


def test_points_the_cli_config_at_the_owner_area_after_migrating(cfg):
    _seed_project(cfg.root)
    result = migrate_to_accounts(cfg)

    assert owner_config(cfg).library_dir == user_path(cfg, result.owner_id, "library")


def test_points_the_cli_config_at_the_project_root_before_migrating(cfg):
    _seed_project(cfg.root)

    assert owner_config(cfg).library_dir == cfg.root / "library"


# ---------- area_config ----------


def _owner(cfg):
    migrate(cfg)
    with connect(cfg) as connection:
        return create_user(connection, kind="owner", email="dono@exemplo.com")


def test_keeps_the_owner_on_the_project_root_while_the_old_library_has_content(cfg):
    _seed_project(cfg.root)
    owner = _owner(cfg)

    assert area_config(cfg, owner).library_dir == cfg.root / "library"


def test_sends_a_fresh_owner_to_their_own_area(cfg):
    """Instalacao nova nao tem acervo na raiz, e o dono novo nao pode herdar a raiz.

    A area so nasce no primeiro upload, entao perguntar se ela existe devolvia a
    raiz para todo dono recem-criado - e o painel gravava fora de qualquer conta.
    """
    owner = _owner(cfg)

    assert area_config(cfg, owner).library_dir == user_path(cfg, owner.id, "library")


def test_ignores_an_empty_library_left_behind_by_the_migration(cfg):
    owner = _owner(cfg)
    (cfg.root / "library").mkdir()

    assert area_config(cfg, owner).library_dir == user_path(cfg, owner.id, "library")


def test_never_sends_a_tester_to_the_project_root(cfg):
    _seed_project(cfg.root)
    migrate(cfg)
    with connect(cfg) as connection:
        tester = create_user(connection, kind="tester")

    assert area_config(cfg, tester).library_dir == user_path(cfg, tester.id, "library")


# ---------- 8.6 o .env nao desfaz a troca de senha ----------


@pytest.mark.parametrize(
    ("name", "changed_in_panel", "valid_after_restart"),
    [
        ("sem troca pelo painel, o .env manda", False, "do-env-nova-e-comprida"),
        ("depois da troca pelo painel, o .env nao manda mais", True, "do-painel-bem-comprida"),
    ],
)
def test_lets_a_restart_reapply_the_env_password_only_until_the_panel_changes_it(
    cfg, name: str, changed_in_panel: bool, valid_after_restart: str
):
    from .sessions import verify_password

    migrate(cfg)
    with connect(cfg) as connection:
        owner = ensure_owner(connection, "dono@example.com", "do-env-original-comprida")
        if changed_in_panel:
            change_owner_password(connection, owner.id, "do-painel-bem-comprida")

        ensure_owner(connection, "dono@example.com", "do-env-nova-e-comprida")

        assert verify_password(valid_after_restart, password_hash_of(connection, owner.id)), name


def test_refuses_a_short_owner_password(cfg):
    migrate(cfg)
    with connect(cfg) as connection:
        owner = ensure_owner(connection, "dono@example.com", "do-env-original-comprida")
        with pytest.raises(ValueError):
            change_owner_password(connection, owner.id, "curta")
