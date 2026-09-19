"""配置解析:SQLite 相对路径必须按 backend/ 解析,否则换个目录启动就打不开数据库。"""
from pathlib import Path

from app.config import BACKEND_DIR, _absolutize_sqlite_url


def test_relative_sqlite_becomes_absolute():
    """这是真实踩过的坑:在项目根目录启动 uvicorn 会报 unable to open database file。"""
    url = _absolutize_sqlite_url("sqlite:///./data/app.db", BACKEND_DIR)
    assert url == f"sqlite:///{BACKEND_DIR / 'data' / 'app.db'}"
    assert url.startswith("sqlite:////"), "绝对路径的 SQLite URL 应该是四个斜杠"


def test_relative_without_dot_prefix_also_absolute():
    url = _absolutize_sqlite_url("sqlite:///data/app.db", BACKEND_DIR)
    assert Path(url.replace("sqlite:///", "")).is_absolute()


def test_parent_directory_created(tmp_path):
    _absolutize_sqlite_url("sqlite:///nested/deep/x.db", tmp_path)
    assert (tmp_path / "nested" / "deep").is_dir()


def test_absolute_sqlite_untouched():
    url = "sqlite:////var/tmp/already-absolute.db"
    assert _absolutize_sqlite_url(url, BACKEND_DIR) == url


def test_memory_sqlite_untouched():
    assert _absolutize_sqlite_url("sqlite:///:memory:", BACKEND_DIR) == "sqlite:///:memory:"
    assert _absolutize_sqlite_url("sqlite://", BACKEND_DIR) == "sqlite://"


def test_mysql_url_untouched():
    url = "mysql+pymysql://u:p@127.0.0.1:3306/express_agent?charset=utf8mb4"
    assert _absolutize_sqlite_url(url, BACKEND_DIR) == url


def test_knowledge_dir_is_absolute():
    from app.config import settings

    assert settings.knowledge_dir.is_absolute()
    assert settings.data_dir.is_absolute()
