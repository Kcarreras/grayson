"""Workspace discovery, initialization, and path resolution."""

from __future__ import annotations

from pathlib import Path

from grayson.config import CONFIG_FILENAME, CONFIG_TEMPLATE, WorkspaceConfig

GITIGNORE_BLOCK = "\n# grayson session state and cached warehouse data\n.grayson/\n"

REGISTRY_TEMPLATE = "# QA view library registry (managed by `grayson views`)\nviews: []\n"


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._config: WorkspaceConfig | None = None
        self._config_stamp: tuple[int, int] | None = None

    # -- discovery -------------------------------------------------------

    @classmethod
    def find(cls, start: Path | None = None) -> Workspace:
        cur = (start or Path.cwd()).resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / CONFIG_FILENAME).is_file():
                return cls(candidate)
        raise FileNotFoundError(
            f"no {CONFIG_FILENAME} found in {cur} or any parent — run `grayson init` first"
        )

    @classmethod
    def init(cls, path: Path) -> Workspace:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        config_path = path / CONFIG_FILENAME
        if config_path.exists():
            raise FileExistsError(f"{config_path} already exists")
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        (path / ".grayson" / "sessions").mkdir(parents=True, exist_ok=True)
        (path / "knowledge").mkdir(exist_ok=True)
        (path / "views" / "ddl").mkdir(parents=True, exist_ok=True)
        (path / "workflows").mkdir(exist_ok=True)
        (path / "findings_schemas").mkdir(exist_ok=True)
        (path / "records").mkdir(exist_ok=True)
        from grayson.checks import scaffold_checks_dir

        scaffold_checks_dir(path / "checks")
        registry = path / "views" / "registry.yaml"
        if not registry.exists():
            registry.write_text(REGISTRY_TEMPLATE, encoding="utf-8")
        glossary = path / "knowledge" / "glossary.md"
        if not glossary.exists():
            glossary.write_text("# Glossary\n", encoding="utf-8")
        gitignore = path / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if ".grayson/" not in existing:
            gitignore.write_text(existing + GITIGNORE_BLOCK, encoding="utf-8")
        return cls(path)

    # -- config & paths --------------------------------------------------

    @property
    def config(self) -> WorkspaceConfig:
        """The workspace's grayson.toml, re-read whenever the file changes on disk.

        Long-lived processes (the MCP server an agent harness starts, the
        console) hold one Workspace for their whole life. Caching the parsed
        config forever would mean a guard profile edited in the console after
        the harness launched never reaches sessions the agent starts through
        MCP — they would snapshot the stale numbers. The cache is keyed on the
        file's mtime and size, so an edit through any surface (console, CLI,
        a text editor) is picked up on the next read at the cost of a stat."""
        path = self.root / CONFIG_FILENAME
        try:
            st = path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if self._config is None or stamp != self._config_stamp:
            self._config = WorkspaceConfig.load(path)
            self._config_stamp = stamp
        return self._config

    def reload_config(self) -> WorkspaceConfig:
        """Drop the cached config unconditionally (an on-disk edit is already
        picked up by `config` itself; this forces it regardless of mtime)."""
        self._config = None
        self._config_stamp = None
        return self.config

    @property
    def sessions_dir(self) -> Path:
        return self.root / ".grayson" / "sessions"

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

    @property
    def findings_schemas_dir(self) -> Path:
        """The team's own findings schemas, beside workflows/ in the library."""
        return self._library_root() / "findings_schemas"

    @property
    def checks_dir(self) -> Path:
        return self._library_root() / "checks"

    @property
    def records_dir(self) -> Path:
        """Published records (accepted findings, verified fixes) — the distilled,
        team-shareable output of sessions, unlike .grayson/ session state."""
        return self._library_root() / "records"

    @property
    def reports_dir(self) -> Path:
        """Report profiles: how session reports render for this team."""
        return self._library_root() / "reports"
