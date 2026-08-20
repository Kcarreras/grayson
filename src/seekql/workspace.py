"""Workspace discovery, initialization, and path resolution."""

from __future__ import annotations

from pathlib import Path

from seekql.config import CONFIG_FILENAME, CONFIG_TEMPLATE, WorkspaceConfig

GITIGNORE_BLOCK = "\n# seekql session state and cached warehouse data\n.seekql/\n"

REGISTRY_TEMPLATE = "# QA view library registry (managed by `seekql views`)\nviews: []\n"


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._config: WorkspaceConfig | None = None

    # -- discovery -------------------------------------------------------

    @classmethod
    def find(cls, start: Path | None = None) -> Workspace:
        cur = (start or Path.cwd()).resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / CONFIG_FILENAME).is_file():
                return cls(candidate)
        raise FileNotFoundError(
            f"no {CONFIG_FILENAME} found in {cur} or any parent — run `seekql init` first"
        )

    @classmethod
    def init(cls, path: Path) -> Workspace:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        config_path = path / CONFIG_FILENAME
        if config_path.exists():
            raise FileExistsError(f"{config_path} already exists")
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        (path / ".seekql" / "sessions").mkdir(parents=True, exist_ok=True)
        (path / "knowledge").mkdir(exist_ok=True)
        (path / "views" / "ddl").mkdir(parents=True, exist_ok=True)
        (path / "workflows").mkdir(exist_ok=True)
        registry = path / "views" / "registry.yaml"
        if not registry.exists():
            registry.write_text(REGISTRY_TEMPLATE, encoding="utf-8")
        glossary = path / "knowledge" / "glossary.md"
        if not glossary.exists():
            glossary.write_text("# Glossary\n", encoding="utf-8")
        gitignore = path / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if ".seekql/" not in existing:
            gitignore.write_text(existing + GITIGNORE_BLOCK, encoding="utf-8")
        return cls(path)

    # -- config & paths --------------------------------------------------

    @property
    def config(self) -> WorkspaceConfig:
        if self._config is None:
            self._config = WorkspaceConfig.load(self.root / CONFIG_FILENAME)
        return self._config

    @property
    def sessions_dir(self) -> Path:
        return self.root / ".seekql" / "sessions"

    def session_dir(self, session_id: str) -> Path:
        if not session_id or any(c in session_id for c in "/\\.:"):
            raise ValueError(f"invalid session id: {session_id!r}")
        return self.sessions_dir / session_id

    def list_session_ids(self) -> list[str]:
        if not self.sessions_dir.is_dir():
            return []
        return sorted(p.name for p in self.sessions_dir.iterdir() if p.is_dir())

    def _library_root(self) -> Path:
        """Library assets root: linked team library clone if configured, else workspace."""
        lib = self.config.library_path
        if lib is not None:
            if not lib.is_dir():
                raise FileNotFoundError(f"[library] path does not exist: {lib}")
            return lib
        return self.root

    @property
    def knowledge_dir(self) -> Path:
        return self._library_root() / "knowledge"

    @property
    def views_dir(self) -> Path:
        return self._library_root() / "views"

    @property
    def workflows_dir(self) -> Path:
        return self._library_root() / "workflows"
