"""Drift guard for the ONE docker-compose.yml (CLAUDE.md "Engineering rules"), like test_policy_consistency.py.

The ports and profiles of every service are written down in four places on purpose: docker-compose.yml itself, its
header table, docs/backend-services-contract.md ("Services and ports") and docs/RUNNING.md. This fails when they
disagree, and on the compose conventions the docs promise:
  - every long-running service has a healthcheck (in compose or its Dockerfile) and a non-root USER;
  - every depends_on target exists, a `service_healthy` target has a healthcheck, and a dependency that some of
    the dependent's profiles don't start is `required: false`;
  - every published port binds ${BIND_ADDR:-127.0.0.1} and its container port is the one the Dockerfile EXPOSEs;
  - every built folder has a .dockerignore (<Dockerfile>.dockerignore when the context is the repo root).
Static: PyYAML + pytest only (no Docker). `make test-compose`; CI also runs `docker compose config -q` per profile.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
SERVICES: dict = COMPOSE["services"]
IMAGE_ONLY_FOLDERS = {"db": "db/", "web": "web/"}          # services without a build, by the folder the contract lists them under
PORT_RE = re.compile(r"^\$\{BIND_ADDR:-127\.0\.0\.1\}:(?P<host>\$\{[A-Z_]+:-(?P<hdef>\d+)\}|\d+):(?P<target>\d+)$")


def _profiles(svc: str) -> tuple[str, ...]:
    return tuple(SERVICES[svc].get("profiles") or ())


def _ports(svc: str) -> list[re.Match]:
    out = []
    for spec in SERVICES[svc].get("ports") or []:
        m = PORT_RE.match(str(spec))
        assert m, "%s: published port %r must bind ${BIND_ADDR:-127.0.0.1}" % (svc, spec)
        out.append(m)
    return out


def host_port(svc: str) -> int | None:
    """The default host port (the number the docs quote), or None for a service that publishes nothing."""
    ports = _ports(svc)
    if not ports:
        return None
    m = ports[0]
    return int(m.group("hdef") or m.group("host"))


def build_of(svc: str) -> tuple[Path, Path] | None:
    """(context dir, Dockerfile) for a built service, else None."""
    b = SERVICES[svc].get("build")
    if b is None:
        return None
    b = {"context": b} if isinstance(b, str) else b
    ctx = ROOT / b.get("context", ".")
    return ctx, ctx / b.get("dockerfile", "Dockerfile")


def folder_of(svc: str) -> str | None:
    built = build_of(svc)
    if built is None:
        return IMAGE_ONLY_FOLDERS.get(svc)
    return built[1].parent.relative_to(ROOT).as_posix() + "/"


def dockerfile_text(svc: str) -> str:
    built = build_of(svc)
    return built[1].read_text() if built else ""


def one_shot(svc: str) -> bool:
    return str(SERVICES[svc].get("restart", "")) == "no"


def has_healthcheck(svc: str) -> bool:
    hc = SERVICES[svc].get("healthcheck")
    if hc is not None:
        return not hc.get("disable", False)
    return bool(re.search(r"^HEALTHCHECK\s+(?!NONE)", dockerfile_text(svc), re.M))


# ── the three written-down tables ───────────────────────────────────────────────────────────────
def header_table() -> dict[str, tuple[int | None, tuple[str, ...]]]:
    """docker-compose.yml header: `# <service> <port|-> <profiles>  <what>` rows (continuation lines skipped)."""
    lines = COMPOSE_TEXT.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"^# Service\s+Port\s+Profiles", ln))
    rows = {}
    for ln in lines[start + 2:]:
        if not ln.startswith("# ") or ln.strip() == "#":
            break
        m = re.match(r"^# (?P<svc>[a-z][a-z0-9-]*)\s+(?P<port>\d+|-)\s+(?P<prof>[a-z0-9]+(?:, [a-z0-9]+)*)(?:\s{2,}|$)",
                     ln)
        if m:
            rows[m["svc"]] = (None if m["port"] == "-" else int(m["port"]),
                              tuple(p.strip() for p in m["prof"].split(",")))
    return rows


def _md_rows(text: str, heading_re: str) -> list[list[str]]:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.search(heading_re, ln))
    rows = []
    for ln in lines[start + 2:]:
        if not ln.startswith("|"):
            break
        rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
    return rows


def contract_table() -> dict[str, int]:
    """docs/backend-services-contract.md "Services and ports": folder -> port (rows with a real folder)."""
    text = (ROOT / "docs" / "backend-services-contract.md").read_text()
    out = {}
    for name, folder, port, *_ in _md_rows(text, r"^\| Service \| Folder \| Port \|"):
        f = folder.strip("`")
        if f == "n/a":
            continue
        out[f] = int(re.match(r"\d+", port).group())
    return out


def running_table() -> dict[str, tuple[int | None, tuple[str, ...]]]:
    """docs/RUNNING.md service table: service -> (port, profiles)."""
    text = (ROOT / "docs" / "RUNNING.md").read_text()
    out = {}
    for svc, port, profiles, *_ in _md_rows(text, r"^\| Service \| Port \| Profiles \|"):
        m = re.match(r"\d+", port)
        out[svc.strip("`")] = (int(m.group()) if m else None, tuple(p.strip() for p in profiles.split(",")))
    return out


# ── tests ───────────────────────────────────────────────────────────────────────────────────────
def test_tables_parse():
    assert len(header_table()) >= 10 and len(contract_table()) >= 9 and len(running_table()) >= 10


def test_header_table_matches_services_ports_and_profiles():
    header = header_table()
    assert set(header) == set(SERVICES), "header table vs services: %s" % sorted(set(header) ^ set(SERVICES))
    for svc, (port, profiles) in header.items():
        assert port == host_port(svc), "%s: header says port %s, compose publishes %s" % (svc, port, host_port(svc))
        assert set(profiles) == set(_profiles(svc)), "%s: header profiles %s vs %s" % (svc, profiles,
                                                                                        _profiles(svc))


def test_running_md_table_matches_services_ports_and_profiles():
    running = running_table()
    assert set(running) == set(SERVICES), "RUNNING.md table vs services: %s" % sorted(set(running) ^ set(SERVICES))
    for svc, (port, profiles) in running.items():
        assert port == host_port(svc), "%s: RUNNING.md says port %s, compose publishes %s" % (svc, port,
                                                                                               host_port(svc))
        assert set(profiles) == set(_profiles(svc)), "%s: RUNNING.md profiles %s vs %s" % (svc, profiles,
                                                                                            _profiles(svc))


def test_contract_ports_table_matches_compose():
    contract = contract_table()
    published = {folder_of(s): host_port(s) for s in SERVICES if host_port(s) is not None}
    assert None not in published, "a published service has no folder mapping (add it to IMAGE_ONLY_FOLDERS)"
    assert published == contract, "contract vs compose (folder -> port): only in contract %s, only in compose %s" % (
        sorted(set(contract.items()) - set(published.items())), sorted(set(published.items()) - set(contract.items())))


@pytest.mark.parametrize("svc", sorted(SERVICES))
def test_every_long_running_service_has_a_healthcheck(svc):
    if one_shot(svc):
        return
    assert has_healthcheck(svc), "%s has no healthcheck in compose or in its Dockerfile" % svc


@pytest.mark.parametrize("svc", sorted(SERVICES))
def test_depends_on_targets_exist_and_match_profiles(svc):
    deps = SERVICES[svc].get("depends_on") or {}
    if isinstance(deps, list):
        deps = {d: {"condition": "service_started"} for d in deps}
    for dep, spec in deps.items():
        assert dep in SERVICES, "%s depends on undefined service %s" % (svc, dep)
        cond = spec.get("condition", "service_started")
        if cond == "service_healthy":
            assert has_healthcheck(dep), "%s waits for %s to be healthy, but it has no healthcheck" % (svc, dep)
        if cond == "service_completed_successfully":
            assert one_shot(dep), "%s waits for %s to complete, but it restarts" % (svc, dep)
        missing = set(_profiles(svc)) - set(_profiles(dep))
        if missing:
            assert spec.get("required") is False, (
                "%s depends on %s, which profile(s) %s don't start: mark it `required: false`"
                % (svc, dep, sorted(missing)))


@pytest.mark.parametrize("svc", sorted(s for s in SERVICES if SERVICES[s].get("ports")))
def test_container_port_is_the_exposed_port(svc):
    exposed = {int(p) for ln in re.findall(r"^EXPOSE\s+(.+)$", dockerfile_text(svc), re.M)
               for p in re.findall(r"\d+", ln)}
    if not exposed:          # image-only service (db): nothing to compare with
        assert build_of(svc) is None, "%s: Dockerfile has no EXPOSE" % svc
        return
    for m in _ports(svc):
        assert int(m.group("target")) in exposed, "%s publishes container port %s, Dockerfile EXPOSEs %s" % (
            svc, m.group("target"), sorted(exposed))


@pytest.mark.parametrize("svc", sorted(s for s in SERVICES if SERVICES[s].get("build")))
def test_built_images_run_as_non_root_and_have_a_dockerignore(svc):
    ctx, dockerfile = build_of(svc)
    users = re.findall(r"^USER\s+(\S+)", dockerfile.read_text(), re.M)
    assert users, "%s: Dockerfile has no USER" % svc
    assert users[-1].split(":")[0] not in ("root", "0"), "%s runs as root" % svc
    assert (ctx / ".dockerignore").is_file() or Path(str(dockerfile) + ".dockerignore").is_file(), (
        "%s: no .dockerignore for context %s" % (svc, ctx.relative_to(ROOT)))
