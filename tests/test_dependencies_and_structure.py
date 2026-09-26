"""T-13 and structural invariants.

Audit references: C-2 (``openai`` imported but undeclared), M-5 (unused
dependencies and imports), the ``bot -> tasks -> bot`` circular dependency,
and the layering requirements TR-2 / TR-3.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

# Distribution name -> import name, where they differ.
IMPORT_NAME = {
    "pydantic-settings": "pydantic_settings",
}


def _normalise(dep: str) -> str:
    """Reduce a distribution name to the name used in an import statement."""
    base = dep.split("[", 1)[0].strip().lower()
    return IMPORT_NAME.get(base, base.replace("-", "_"))


def _declared_requirements() -> dict[str, str]:
    """Parse pinned requirements, ignoring comments and the -r include."""
    pins: dict[str, str] = {}
    for raw in (ROOT / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        for sep in ("==", ">=", "<=", "~="):
            if sep in line:
                name, version = line.split(sep, 1)
                pins[name.strip()] = version.strip()
                break
    return pins


def _imported_top_level_packages() -> set[str]:
    """Every third-party top-level module imported under app/."""
    stdlib = set(sys.stdlib_module_names)
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in stdlib and top != "app":
                        found.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import within app
                    continue
                if node.module:
                    top = node.module.split(".")[0]
                    if top not in stdlib and top != "app":
                        found.add(top)
    return found


# --- FR-2.1 / T-13: every import is declared --------------------------------

def test_t13_every_imported_package_is_declared():
    declared = {_normalise(d) for d in _declared_requirements()}
    imported = _imported_top_level_packages()

    missing = {pkg for pkg in imported if pkg not in declared}
    assert not missing, (
        f"imported but not declared in requirements.txt: {sorted(missing)}"
    )


def test_t13_openai_is_declared():
    """The specific defect: openai was imported but never declared (C-2)."""
    assert "openai" in _declared_requirements(), (
        "openai is imported by app/ but missing from requirements.txt"
    )


def test_t13_no_unused_imports_in_application_source():
    """M-5: unused imports were flagged; ruff enforces this statically."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "app", "--select", "F401", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 127 or "No module named ruff" in result.stderr:
        pytest.skip("ruff is not installed in this environment")
    assert result.returncode == 0, result.stdout


# --- FR-2.3: unused dependencies are removed or justified -----------------

def test_dependencies_without_imports_are_justified():
    """Every declared dependency must be imported or carry a written reason.

    A justification may appear on the dependency's own line or in the comment
    block immediately above it.
    """
    declared = set(_declared_requirements())
    imported = _imported_top_level_packages()

    # Map each dependency to the comment block that precedes it.
    context: dict[str, str] = {}
    pending: list[str] = []
    for raw in (ROOT / "requirements.txt").read_text().splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            pending.append(stripped)
            continue
        line = stripped.split("#", 1)[0].strip()
        if line.startswith("-"):
            continue
        for sep in ("==", ">=", "<=", "~="):
            if sep in line:
                name = line.split(sep, 1)[0].strip()
                context[name] = "\n".join(pending) + "\n" + stripped
                break
        pending = []

    keywords = re.compile(r"required|justified|reserved|declared|transitive", re.I)
    unjustified = []
    for dep in declared:
        if _normalise(dep) in imported:
            continue
        base = re.escape(dep.split("[", 1)[0])
        nearby = context.get(dep, "")
        if re.search(rf"\b{base}\b", nearby) and keywords.search(nearby):
            continue
        unjustified.append(dep)

    assert not unjustified, (
        f"declared but neither imported nor justified: {sorted(unjustified)}"
    )


# --- Structural: no bot <-> tasks circular dependency ----------------------

def test_bot_layer_does_not_import_tasks_at_module_scope():
    """The original cycle was bot/handlers -> tasks -> bot/dispatcher.

    Only module-level imports are checked. A function-local import is allowed
    and is in fact the mechanism that breaks the cycle at runtime.
    """
    for path in (APP / "bot").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:  # direct children of the module only
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.tasks"), (
                    f"{path.relative_to(ROOT)} imports app.tasks at module scope"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.tasks"), (
                        f"{path.relative_to(ROOT)} imports app.tasks at module scope"
                    )


def test_bot_layer_may_import_tasks_lazily():
    """Guard the fix: the tasks import must stay function-local."""
    handler_source = (APP / "bot" / "handlers.py").read_text()
    assert "from app.tasks.research_task import process_research" in handler_source
    tree = ast.parse(handler_source)
    module_level = [
        node for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
        and node.module.startswith("app.tasks")
    ]
    assert not module_level, "the tasks import moved back to module scope"


def test_tasks_do_not_import_bot_handlers():
    for path in (APP / "tasks").rglob("*.py"):
        text = path.read_text()
        assert "bot.handlers" not in text, (
            f"{path.relative_to(ROOT)} imports bot handlers"
        )


def test_layers_exist():
    """TR-2: the layering the phase document specifies is present."""
    for layer in ("domain", "services", "infrastructure"):
        assert (APP / layer).is_dir(), f"app/{layer}/ is missing"
        assert (APP / layer / "__init__.py").exists()


def test_handlers_and_tasks_contain_no_provider_sdk_usage():
    """Handlers and tasks must delegate, not talk to the provider directly."""
    for path in [*(APP / "bot").rglob("*.py"), *(APP / "tasks").rglob("*.py")]:
        text = path.read_text()
        assert "chat.completions" not in text, (
            f"{path.relative_to(ROOT)} calls the provider SDK directly"
        )


def test_no_module_level_singleton_bot_is_constructed():
    """C-3's enabler: a Bot built at import time is shared across loops."""
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:  # module level only
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {"bot", "dp", "client"}:
                        value = ast.unparse(node.value)
                        assert "Bot(" not in value and "OpenAI(" not in value, (
                            f"{path.relative_to(ROOT)} constructs {target.id} at module level"
                        )


# --- SR-1: no hardcoded secret anywhere in the repository ------------------

def test_sr1_repository_contains_no_secret_outside_env():
    """The only file permitted to hold a secret value is ``.env``.

    ``tests/`` is excluded deliberately: exercising the token and key code
    paths requires token-shaped fixtures. Those live in the test suite, are
    never used against a real endpoint, and the suite blocks outbound network
    access (see the autouse guard in ``tests/conftest.py``). ``docs/`` is
    excluded because the audit records findings by file path only.
    """
    # ``gsk_`` with an underscore: a hyphen variant matches no real Groq key and
    # silently disabled this scan. Pinned by
    # test_the_secret_scanner_matches_realistic_key_shapes.
    pattern = re.compile(
        r"sk-[A-Za-z0-9_-]{16,}"
        r"|gsk_[A-Za-z0-9_-]{16,}"
        r"|\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"
    )
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        parts = rel.parts
        if parts[0] in {"venv", ".git", "__pycache__", "tests", "docs"}:
            continue
        if rel.name == ".env":
            continue
        if path.suffix in {".pyc", ".so", ".png", ".jpg"}:
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if pattern.search(content):
            offenders.append(str(rel))
    assert not offenders, f"secret-shaped value outside .env: {offenders}"


def test_sr1_network_access_is_blocked_during_tests():
    """The suite must be incapable of calling a real provider or Telegram."""
    conftest = (ROOT / "tests" / "conftest.py").read_text()
    assert "block_outbound_network" in conftest
    assert "autouse=True" in conftest


# --- Dead code: Phase 2 removed the superseded service (AD-026) ------------

def test_the_superseded_research_service_is_gone():
    """`DEVELOPMENT_RULES.md` § 4: dead code must be wired in or removed.

    ``app/services/research.py`` was the request-path service in Phase 1. Phase 2
    replaced it with ``app/services/chat.py``; leaving both would have meant two
    services racing to own the same request path.
    """
    assert not (APP / "services" / "research.py").exists(), (
        "app/services/research.py was superseded by app/services/chat.py (AD-026) "
        "and must not come back"
    )
    source = (APP / "services" / "chat.py").read_text()
    assert "ResearchService" not in source


def test_the_retained_phase3_seams_are_documented_as_unwired():
    """The two functions kept for Phase 3 must say so where they are defined.

    A silent unwired function is the rot `DEVELOPMENT_RULES.md` § 4 warns about.
    Asserting the documentation exists is a cheap guard; attempting a general
    dead-code detector with AST heuristics produced false positives on decorated
    handlers, protocol methods, and file-local helpers, so it is not used.
    """
    supervisor = (APP / "agents" / "supervisor.py").read_text()
    assert "no callers" in supervisor or "unwired" in supervisor.lower()
    assert "Phase 3" in supervisor

    llm = (APP / "infrastructure" / "llm.py").read_text()
    assert "Unwired: not called by the Phase 2 request path" in llm, (
        "the LLM seam kept for Phase 3 is undocumented"
    )

    formatting = (APP / "services" / "formatting.py").read_text()
    assert "Not called by the Phase 2 request path" in formatting
    assert "Phase 3" in formatting


def test_the_request_path_does_not_use_the_phase3_decomposition_seam():
    """Chat goes through `complete`; `analyze_query` is for Phase 3."""
    for module in ("services/chat.py", "tasks/research_task.py", "bot/handlers.py"):
        source = (APP / module).read_text()
        assert "analyze_query" not in source, f"{module} uses the Phase 3 seam"
        assert "generate_final_answer" not in source
