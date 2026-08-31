#!/usr/bin/env python3
"""Build deterministic, phase-gated Danse public and institutional artifacts."""

from __future__ import annotations

_bootstrap_sys = __import__("sys")
_bootstrap_os = __import__("os")
if getattr(getattr(_bootstrap_os, "__spec__", None), "origin", None) not in {
    "built-in",
    "frozen",
}:
    raise RuntimeError("release builder requires the frozen OS path bootstrap")
_bootstrap_scripts = _bootstrap_os.path.realpath(
    _bootstrap_os.path.dirname(__file__)
)
_bootstrap_root = _bootstrap_os.path.dirname(_bootstrap_scripts)
_bootstrap_boundaries = [_bootstrap_root]
_bootstrap_argv_index = 0
_bootstrap_root_value = ""
_bootstrap_root_present = False
while _bootstrap_argv_index < len(_bootstrap_sys.argv):
    _bootstrap_argument = _bootstrap_sys.argv[_bootstrap_argv_index]
    if _bootstrap_argument in {"--r", "--ro", "--roo", "--root"}:
        _bootstrap_root_present = True
        if _bootstrap_argv_index + 1 < len(_bootstrap_sys.argv):
            _bootstrap_argv_index += 1
            _bootstrap_root_value = _bootstrap_sys.argv[_bootstrap_argv_index]
    elif any(
        _bootstrap_argument.startswith(prefix)
        for prefix in ("--r=", "--ro=", "--roo=", "--root=")
    ):
        _bootstrap_root_present = True
        _bootstrap_root_value = _bootstrap_argument.split("=", 1)[1]
    if _bootstrap_root_present:
        _bootstrap_boundaries.append(
            _bootstrap_os.path.realpath(
                _bootstrap_root_value or _bootstrap_os.getcwd()
            )
        )
        _bootstrap_root_value = ""
        _bootstrap_root_present = False
    _bootstrap_argv_index += 1
_bootstrap_prefix = _bootstrap_os.path.realpath(_bootstrap_sys.prefix)
_bootstrap_active_venv = _bootstrap_sys.prefix != _bootstrap_sys.base_prefix
_bootstrap_safe_path: list[str] = []
_bootstrap_argument = ""
_bootstrap_boundary = ""
_bootstrap_entry = ""
_bootstrap_candidate = ""
_bootstrap_common = ""
_bootstrap_inside_repository = False
_bootstrap_prefix_common = ""
for _bootstrap_entry in _bootstrap_sys.path:
    _bootstrap_candidate = _bootstrap_os.path.realpath(
        _bootstrap_entry or _bootstrap_os.getcwd()
    )
    _bootstrap_inside_repository = False
    for _bootstrap_boundary in _bootstrap_boundaries:
        try:
            _bootstrap_common = _bootstrap_os.path.commonpath(
                [_bootstrap_candidate, _bootstrap_boundary]
            )
        except ValueError:
            _bootstrap_common = ""
        if _bootstrap_common == _bootstrap_boundary:
            _bootstrap_inside_repository = True
            break
    try:
        _bootstrap_prefix_common = _bootstrap_os.path.commonpath(
            [_bootstrap_candidate, _bootstrap_prefix]
        )
    except ValueError:
        _bootstrap_prefix_common = ""
    if (
        _bootstrap_inside_repository
        and not (
            _bootstrap_active_venv
            and _bootstrap_prefix_common == _bootstrap_prefix
        )
    ):
        continue
    _bootstrap_safe_path.append(_bootstrap_entry)
_bootstrap_sys.path[:] = _bootstrap_safe_path
del (
    _bootstrap_active_venv,
    _bootstrap_argument,
    _bootstrap_argv_index,
    _bootstrap_boundaries,
    _bootstrap_boundary,
    _bootstrap_candidate,
    _bootstrap_common,
    _bootstrap_entry,
    _bootstrap_inside_repository,
    _bootstrap_os,
    _bootstrap_prefix,
    _bootstrap_prefix_common,
    _bootstrap_root,
    _bootstrap_root_present,
    _bootstrap_root_value,
    _bootstrap_safe_path,
    _bootstrap_scripts,
    _bootstrap_sys,
)

import argparse
import hashlib
import html
import io
import os
import platform
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import types
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

import pypdf
import reportlab
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

def _load_release_contract():
    """Load source bytes only from the contract beside this builder."""
    path = Path(__file__).resolve().with_name("release_contract.py")
    identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module_name = f"danse_release_contract_{identity}"
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    module.__loader__ = None
    module.__spec__ = None
    try:
        source = path.read_bytes()
        code = compile(source, str(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
        required = (
            "EXPECTED_OPPORTUNITY_FROZEN_AT",
            "EXPECTED_OPPORTUNITY_RECEIPT_SHA256",
            "EXPECTED_OPPORTUNITY_SHA256",
            "EXPECTED_SOURCE_EVIDENCE_SHA256",
            "GENERATED_PRODUCT_PATHS",
            "HEX64",
            "MANIFEST",
            "PHASES",
            "PROGRESSIVE_CONTROLS_SCHEMA_PATH",
            "ROOT",
            "SCHEMA",
            "ReleaseError",
            "canonical_json",
            "decode_json_object",
            "load_json",
            "phase_blockers",
            "provenance_git_command",
            "provenance_git_env",
            "reject_git_rewrites",
            "require_commit_object",
            "safe_relative",
            "sha256",
            "source_commit",
            "source_commit_blob",
            "source_file",
            "validate_release",
            "validate_schema",
        )
        if any(not hasattr(module, name) for name in required):
            raise AttributeError("release contract is missing its required API")
    except (ImportError, OSError, RuntimeError, SyntaxError, ValueError, AttributeError) as exc:
        raise RuntimeError("cannot load the sibling release contract") from exc
    return module


_RELEASE_CONTRACT = _load_release_contract()
EXPECTED_OPPORTUNITY_FROZEN_AT = _RELEASE_CONTRACT.EXPECTED_OPPORTUNITY_FROZEN_AT
EXPECTED_OPPORTUNITY_RECEIPT_SHA256 = _RELEASE_CONTRACT.EXPECTED_OPPORTUNITY_RECEIPT_SHA256
EXPECTED_OPPORTUNITY_SHA256 = _RELEASE_CONTRACT.EXPECTED_OPPORTUNITY_SHA256
EXPECTED_SOURCE_EVIDENCE_SHA256 = _RELEASE_CONTRACT.EXPECTED_SOURCE_EVIDENCE_SHA256
GENERATED_PRODUCT_PATHS = _RELEASE_CONTRACT.GENERATED_PRODUCT_PATHS
HEX64 = _RELEASE_CONTRACT.HEX64
MANIFEST = _RELEASE_CONTRACT.MANIFEST
PHASES = _RELEASE_CONTRACT.PHASES
PROGRESSIVE_CONTROLS_SCHEMA_PATH = _RELEASE_CONTRACT.PROGRESSIVE_CONTROLS_SCHEMA_PATH
ROOT = _RELEASE_CONTRACT.ROOT
SCHEMA = _RELEASE_CONTRACT.SCHEMA
ReleaseError = _RELEASE_CONTRACT.ReleaseError
canonical_json = _RELEASE_CONTRACT.canonical_json
decode_json_object = _RELEASE_CONTRACT.decode_json_object
load_json = _RELEASE_CONTRACT.load_json
phase_blockers = _RELEASE_CONTRACT.phase_blockers
provenance_git_command = _RELEASE_CONTRACT.provenance_git_command
provenance_git_env = _RELEASE_CONTRACT.provenance_git_env
reject_git_rewrites = _RELEASE_CONTRACT.reject_git_rewrites
require_commit_object = _RELEASE_CONTRACT.require_commit_object
safe_relative = _RELEASE_CONTRACT.safe_relative
sha256 = _RELEASE_CONTRACT.sha256
source_commit = _RELEASE_CONTRACT.source_commit
source_commit_blob = _RELEASE_CONTRACT.source_commit_blob
source_file = _RELEASE_CONTRACT.source_file
validate_release = _RELEASE_CONTRACT.validate_release
validate_schema = _RELEASE_CONTRACT.validate_schema

ARTIFACT_SCHEMA = "danse.release-build.v1"
ARTIFACT_MANIFEST = "release-build.json"
PROJECT_PAGE_CONTRACT = "danse.project-page.v1"
RELEASE_PAYLOAD_CONTRACT = "danse.release-payload.v1"
REPOSITORY = "organvm/the-thing-without-a-name"
PDF_NAME = "pitch/danse-installation-pitch.pdf"
GENERATED_PATHS = (
    "project/index.html",
    PDF_NAME,
    "accessibility/accessibility.md",
    "accessibility/captions.en.vtt",
    "accessibility/transcript.txt",
    "press/press-kit.md",
    "press/credits.txt",
    "press/posting-calendar.json",
    "media/release-media.json",
)
PROJECT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
    "media-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; "
    "script-src 'none'; frame-src 'none'; child-src 'none'; worker-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)
PROJECT_SITE_URL = "https://organvm.github.io/the-thing-without-a-name/"
PROJECT_CANONICAL_URL = PROJECT_SITE_URL + "project/"
PROJECT_RESOURCES = (
    ("pitch-pdf-copy", "Installation pitch (PDF)"),
    ("accessibility-copy", "Accessibility statement"),
    ("caption-track-copy", "English captions (WebVTT)"),
    ("transcript-copy", "Transcript (plain text)"),
    ("press-kit-copy", "Press kit"),
    ("credits-copy", "Credits"),
)
HTML_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
PROJECT_HEAD_ELEMENTS = {"link", "meta", "style", "title"}


class _ProjectMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: list[str] = []
        self.metas: list[dict[str, str | None]] = []
        self.active_elements: set[str] = set()
        self.event_handlers: set[str] = set()
        self.duplicate_attributes: set[str] = set()
        self.referrer_policy_overrides: set[str] = set()
        self.link_elements: list[dict[str, str | None]] = []
        self.doctypes = 0
        self.html_starts = 0
        self.html_ends = 0
        self.html_attributes: list[dict[str, str | None]] = []
        self.in_head = False
        self.head_starts = 0
        self.head_ends = 0
        self.head_attributes: list[dict[str, str | None]] = []
        self.head_stack: list[str] = []
        self.in_body = False
        self.body_starts = 0
        self.body_ends = 0
        self.structure_errors: set[str] = set()
        self.head_elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.html_starts += 1
            self.html_attributes.append(values)
            if (
                self.html_starts != 1
                or self.html_ends
                or self.head_starts
                or self.body_starts
            ):
                self.structure_errors.add("misordered html start")
        elif tag == "head":
            self.head_starts += 1
            self.head_attributes.append(values)
            if (
                self.html_starts != 1
                or self.html_ends
                or self.head_starts != 1
                or self.head_ends
                or self.body_starts
                or self.in_body
                or self.in_head
            ):
                self.structure_errors.add("misordered head start")
            self.in_head = True
        elif tag == "body":
            self.body_starts += 1
            if (
                self.html_starts != 1
                or self.html_ends
                or self.head_starts != 1
                or self.head_ends != 1
                or self.in_head
                or self.body_starts != 1
                or self.body_ends
                or self.in_body
            ):
                self.structure_errors.add("misordered body start")
            self.in_body = True
        elif self.in_head:
            if not self.head_stack:
                self.head_elements.append((tag, values))
                if tag not in PROJECT_HEAD_ELEMENTS:
                    self.structure_errors.add(f"prohibited head child {tag}")
            if tag not in HTML_VOID_ELEMENTS:
                self.head_stack.append(tag)
        elif not self.in_body:
            self.structure_errors.add(f"element outside head or body: {tag}")
        element_id = values.get("id")
        if element_id:
            self.ids.add(element_id)
        href = values.get("href")
        if tag == "a" and href:
            self.hrefs.append(href)
        if tag == "meta":
            self.metas.append(values)
        if tag == "link":
            self.link_elements.append(values)
        if tag in {
            "audio",
            "base",
            "embed",
            "form",
            "iframe",
            "img",
            "input",
            "math",
            "noscript",
            "object",
            "picture",
            "script",
            "source",
            "svg",
            "template",
            "track",
            "video",
        }:
            self.active_elements.add(tag)
        self.event_handlers.update(
            name for name, _value in attrs if name.lower().startswith("on")
        )
        self.referrer_policy_overrides.update(
            value or "<empty>"
            for name, value in attrs
            if name.lower() == "referrerpolicy"
            and (value or "").lower() != "no-referrer"
        )
        names = [name.lower() for name, _value in attrs]
        self.duplicate_attributes.update(
            name for name in names if names.count(name) > 1
        )

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in HTML_VOID_ELEMENTS:
            self.structure_errors.add(f"self-closing non-void element {tag}")
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "html":
            self.html_ends += 1
            if (
                self.html_starts != 1
                or self.html_ends != 1
                or self.in_head
                or self.in_body
                or self.head_ends != 1
                or self.body_ends != 1
            ):
                self.structure_errors.add("misordered html end")
        elif tag == "head":
            self.head_ends += 1
            if not self.in_head:
                self.structure_errors.add("unmatched head end")
            if self.head_stack:
                self.structure_errors.add("unclosed head descendant")
                self.head_stack.clear()
            self.in_head = False
        elif tag == "body":
            self.body_ends += 1
            if not self.in_body or self.body_ends != 1:
                self.structure_errors.add("misordered body end")
            self.in_body = False
        elif self.in_head and tag not in HTML_VOID_ELEMENTS:
            if not self.head_stack or self.head_stack[-1] != tag:
                self.structure_errors.add(f"mismatched head descendant {tag}")
            else:
                self.head_stack.pop()
        elif not self.in_body:
            self.structure_errors.add(f"element end outside head or body: {tag}")

    def handle_data(self, data: str) -> None:
        if self.in_head and not self.head_stack and data.strip():
            self.structure_errors.add("non-whitespace head text")
        elif not self.in_head and not self.in_body and data.strip():
            self.structure_errors.add("text outside head or body")

    def handle_decl(self, decl: str) -> None:
        self.doctypes += 1
        if (
            decl.lower() != "doctype html"
            or self.doctypes != 1
            or self.html_starts
            or self.head_starts
            or self.body_starts
        ):
            self.structure_errors.add("invalid or misordered doctype")

    def handle_pi(self, data: str) -> None:
        self.structure_errors.add("processing instruction")


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _repo_evidence_url(commit: str, path: str) -> str:
    return f"https://github.com/{REPOSITORY}/blob/{commit}/{quote(path)}"


def _status(value: str) -> str:
    return f'<span class="status status-{_h(value)}">{_h(value.replace("-", " "))}</span>'


def project_security_contract(manifest: dict, phase: str) -> dict:
    """Bind project discovery metadata to the canonical site and cleared media."""
    if phase not in PHASES:
        raise ReleaseError(f"unknown project security phase: {phase}")
    identity = manifest["identity"]
    project_contract = manifest["artifact_contracts"]["project_page"]
    if project_contract != PROJECT_PAGE_CONTRACT:
        raise ReleaseError(f"unsupported project-page contract: {project_contract}")
    canonical = identity["canonical_url"] + identity["project_path"]
    if identity["canonical_url"] != PROJECT_SITE_URL or canonical != PROJECT_CANONICAL_URL:
        raise ReleaseError("release manifest project URL drifted from the canonical site")
    social_card = next(
        (
            medium
            for medium in manifest["media"]
            if medium["id"] == "project-social-card"
            and phase in medium["required_for"]
            and medium["status"] == "ready"
            and medium["clearance"]["status"] == "cleared"
            and medium["source"] is not None
        ),
        None,
    )
    social_image = None
    if social_card is not None:
        source = social_card["source"]
        destination = safe_relative(
            source["destination"], "project social-card destination"
        )
        social_image = {
            "url": PROJECT_SITE_URL + destination,
            "path": destination,
            "bytes": source["bytes"],
            "sha256": source["sha256"],
        }
    return {
        "project_contract": project_contract,
        "canonical_url": canonical,
        "social_image": social_image,
    }


def _project_html_v1(manifest: dict, phase: str, commit: str) -> bytes:
    identity = manifest["identity"]
    copy = manifest["copy"]
    installation = manifest["installation"]
    accessibility = manifest["accessibility"]
    press = manifest["press"]
    draft = phase == "draft"
    security_contract = project_security_contract(manifest, phase)
    canonical = security_contract["canonical_url"]
    reference = installation["reference_contract"]
    twin_url = _repo_evidence_url(commit, reference["digital_twin"]["path"])
    gates_url = _repo_evidence_url(commit, reference["gate_ledger"]["path"])
    physical_gates = "".join(
        f"<li>{_h(gate.replace('-', ' '))}</li>" for gate in reference["blocked_gates"]
    )

    flow = []
    for node in installation["system_flow"]:
        targets = ", ".join(node["feeds"]) if node["feeds"] else "room output"
        flow.append(
            '<li class="flow-node">'
            f'<p class="flow-label">{_h(node["label"])}</p>'
            f'<p>{_h(node["detail"])}</p>'
            f'<p class="flow-target">feeds: {_h(targets)}</p>'
            "</li>"
        )

    requirements = []
    for item in installation["spatial_requirements"]:
        requirements.append(
            "<li>"
            f'<div><strong>{_h(item["item"])}</strong>{_status(item["status"])}</div>'
            f'<p>{_h(item["detail"])}</p>'
            "</li>"
        )

    rider = []
    for item in installation["technical_rider"]:
        rider.append(
            "<li>"
            f'<div><strong>{_h(item["item"])}</strong>{_status(item["status"])}</div>'
            f'<p>{_h(item["detail"])}</p>'
            "</li>"
        )

    claims = []
    for claim in manifest["claims"]:
        evidence = claim["evidence"]
        evidence_link = ""
        if evidence:
            url = _repo_evidence_url(commit, evidence["path"])
            evidence_link = f' <a href="{_h(url)}">Evidence</a>'
        claims.append(
            "<li>"
            f'<div><strong>{_h(claim["text"])}</strong>{_status(claim["status"])}</div>'
            f'<p>{_h(evidence["summary"]) if evidence else "Evidence gate remains open."}{evidence_link}</p>'
            "</li>"
        )

    credit_items = []
    for credit in manifest["credits"]:
        name = credit["name"] or "Name withheld pending clearance"
        credit_items.append(
            "<li>"
            f'<div><strong>{_h(credit["role"])}</strong>{_status(credit["status"])}</div>'
            f'<p>{_h(name)}. {_h(credit["note"])}</p>'
            "</li>"
        )

    open_gates = []
    for gate in manifest["gates"]:
        if gate["state"] == "pending":
            open_gates.append(
                "<li>"
                f'<div><strong>{_h(gate["id"])}</strong>{_status(gate["state"])}</div>'
                f'<p>{_h(gate["action"])} Owner: {_h(gate["owner"])}; issue #{gate["issue"]}.</p>'
                "</li>"
            )

    media = []
    for medium in manifest["media"]:
        media.append(
            "<li>"
            f'<div><strong>{_h(medium["label"])}</strong>{_status(medium["status"])}</div>'
            f'<p>{_h(medium["kind"])}; clearance: {_h(medium["clearance"]["status"])}; '
            f'required for {_h(", ".join(medium["required_for"]))}.</p>'
            "</li>"
        )

    generated_products = []
    for product in manifest["products"]:
        generated_products.append(
            "<li>"
            f'<div><strong>{_h(product["label"])}</strong>{_status(product["status"])}</div>'
            f'<p>Deterministically generated at <code>{_h(product["path"])}</code> from this manifest; no prebuilt source copy is accepted.</p>'
            "</li>"
        )

    products_by_id = {product["id"]: product for product in manifest["products"]}
    resources = []
    for product_id, label in PROJECT_RESOURCES:
        product = products_by_id[product_id]
        resources.append(
            f'<li><a href="../{_h(product["path"])}">{_h(label)}</a></li>'
        )

    links = "".join(
        f'<li><a href="{_h(link["url"])}">{_h(link["label"])}</a></li>'
        for link in press["canonical_links"]
    )
    interaction = "".join(f"<li>{_h(item)}</li>" for item in installation["interaction_model"])
    draft_banner = (
        '<aside class="draft" role="status"><strong>Draft - not for publication.</strong> '
        "Human, rights, media, installation, and release evidence gates remain open.</aside>"
        if draft
        else ""
    )
    robots = '<meta name="robots" content="noindex,nofollow">' if draft else '<meta name="robots" content="index,follow">'
    social_image_record = security_contract["social_image"]
    social_image = social_image_record["url"] if social_image_record else None
    social_meta = (
        f'<meta property="og:image" content="{_h(social_image)}">\n'
        f'  <meta name="twitter:card" content="summary_large_image">'
        if social_image
        else '<meta name="twitter:card" content="summary">'
    )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta http-equiv="Content-Security-Policy" content="{PROJECT_CSP}">
  <meta name="referrer" content="no-referrer">
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  {robots}
  <title>{_h(identity['public_title'])} | Project</title>
  <meta name="description" content="{_h(copy['logline'])}">
  <meta property="og:title" content="{_h(identity['public_title'])}">
  <meta property="og:description" content="{_h(copy['logline'])}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{_h(canonical)}">
  {social_meta}
  <link rel="canonical" href="{_h(canonical)}">
  <style>
    :root {{ color-scheme: dark; --ink:#f4f0e7; --muted:#b9b6ad; --bg:#0c1014; --panel:#171c22; --line:#39414a; --accent:#ed7745; --ok:#8fcf9f; --pending:#f1bc64; }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; background:var(--bg); }}
    body {{ margin:0; color:var(--ink); background:radial-gradient(circle at 80% 10%,#25202a 0,transparent 34rem),var(--bg); font:17px/1.6 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    a {{ color:var(--ink); text-underline-offset:.22em; }}
    a:hover,a:focus-visible {{ color:var(--accent); }}
    .skip {{ position:absolute; left:1rem; top:-5rem; z-index:10; padding:.7rem 1rem; background:var(--ink); color:var(--bg); }}
    .skip:focus {{ top:1rem; }}
    .shell {{ width:min(74rem,calc(100% - 2rem)); margin:auto; }}
    header {{ min-height:82svh; display:grid; align-content:end; padding:clamp(5rem,12vw,10rem) 0 4rem; border-bottom:1px solid var(--line); }}
    .eyebrow,.kicker {{ color:var(--accent); text-transform:uppercase; letter-spacing:.13em; font-size:.76rem; font-weight:700; }}
    h1 {{ max-width:13ch; margin:.3rem 0 1rem; font:700 clamp(3rem,9vw,7.6rem)/.9 ui-serif,Georgia,serif; letter-spacing:-.055em; }}
    .lead {{ max-width:45rem; color:var(--muted); font-size:clamp(1.15rem,2vw,1.45rem); }}
    .actions {{ display:flex; flex-wrap:wrap; gap:.7rem; margin-top:1.5rem; }}
    .button {{ display:inline-block; padding:.75rem 1rem; border:1px solid var(--line); border-radius:99rem; text-decoration:none; }}
    .button.primary {{ background:var(--ink); color:var(--bg); border-color:var(--ink); }}
    .draft {{ margin:1rem 0 0; border:1px solid var(--pending); background:#2e2618; padding:.9rem 1rem; }}
    main section {{ padding:4rem 0; border-bottom:1px solid var(--line); }}
    h2 {{ margin:0 0 1.25rem; font:600 clamp(2rem,5vw,4rem)/1 ui-serif,Georgia,serif; letter-spacing:-.03em; }}
    h3 {{ margin:0 0 .5rem; font-size:1rem; text-transform:uppercase; letter-spacing:.1em; }}
    .grid {{ display:grid; grid-template-columns:minmax(0,1.1fr) minmax(18rem,.9fr); gap:clamp(2rem,6vw,6rem); align-items:start; }}
    .panel {{ padding:1.4rem; background:var(--panel); background:color-mix(in srgb,var(--panel) 88%,transparent); border:1px solid var(--line); border-radius:.65rem; }}
    ul.clean,.flow {{ list-style:none; padding:0; margin:0; }}
    ul.clean > li {{ padding:1rem 0; border-top:1px solid var(--line); }}
    ul.clean > li:first-child {{ border-top:0; padding-top:0; }}
    ul.clean p {{ margin:.35rem 0 0; color:var(--muted); }}
    .flow {{ display:grid; gap:.7rem; }}
    .flow-node {{ position:relative; padding:1rem 1rem 1rem 1.2rem; border-left:.25rem solid var(--accent); background:var(--panel); }}
    .flow-label {{ margin:0; font-weight:700; }}
    .flow-node p {{ margin:.2rem 0; }}
    .flow-target {{ color:var(--muted); font-size:.85rem; }}
    code {{ overflow-wrap:anywhere; }}
    .status {{ float:right; margin-left:.5rem; padding:.12rem .45rem; border:1px solid currentColor; border-radius:99rem; font-size:.68rem; line-height:1.3; letter-spacing:.06em; text-transform:uppercase; }}
    .status-verified,.status-cleared,.status-ready,.status-satisfied {{ color:var(--ok); }}
    .status-pending,.status-proposed {{ color:var(--pending); }}
    footer {{ padding:2rem 0 calc(2rem + env(safe-area-inset-bottom)); color:var(--muted); font-size:.85rem; }}
    @media (max-width:720px) {{ .grid {{ grid-template-columns:1fr; }} header {{ min-height:72svh; }} .status {{ float:none; display:inline-block; }} }}
    @media (prefers-reduced-motion:reduce) {{ html {{ scroll-behavior:auto; }} *,*::before,*::after {{ animation:none!important; transition:none!important; }} }}
  </style>
</head>
<body>
  <a class="skip" href="#content">Skip to project information</a>
  <div class="shell">
    {draft_banner}
    <header>
      <p class="eyebrow">{_h(copy['eyebrow'])}</p>
      <h1>{_h(identity['canonical_title'])}</h1>
      <p class="lead">{_h(copy['logline'])}</p>
      <nav class="actions" aria-label="Primary">
        <a class="button primary" href="../">Enter the live artwork</a>
        <a class="button" href="#access">Accessibility</a>
        <a class="button" href="#resources">Resources</a>
        <a class="button" href="#evidence">Evidence and release state</a>
      </nav>
    </header>
    <main id="content">
      <section id="cubism" aria-labelledby="concept-title"><div class="grid"><div><p class="kicker">Concept / Cubism</p><h2 id="concept-title">One camera. Many instants. A room in depth.</h2><p>{_h(copy['concept'])}</p></div><aside class="panel"><h3>Installation premise</h3><p>{_h(copy['installation_concept'])}</p></aside></div></section>
      <section id="glitch" aria-labelledby="system-title"><div class="grid"><div><p class="kicker">System / Glitch</p><h2 id="system-title">One contract across every form</h2><p>{_h(copy['technical_summary'])}</p></div><ol class="flow" aria-label="System flow">{''.join(flow)}</ol></div></section>
      <section id="ballet-score" aria-labelledby="room-title"><div class="grid"><div><p class="kicker">Ballet / Score / Room</p><h2 id="room-title">Spatial requirements</h2><ul class="clean">{''.join(requirements)}</ul></div><div><h3>Interaction model</h3><ul>{interaction}</ul></div></div></section>
      <section aria-labelledby="rider-title"><p class="kicker">Technical rider</p><h2 id="rider-title">Designed for validation, not guesswork</h2><ul class="clean">{''.join(rider)}</ul></section>
      <section id="installation-contract" aria-labelledby="installation-contract-title"><div class="grid"><div><p class="kicker">Reference contract</p><h2 id="installation-contract-title">A measured room is still required</h2><p>The release binds reference simulation <strong>{_h(reference['spec_id'])}</strong> at contract digest <code>{_h(reference['spec_contract_sha256'])}</code>. It is a deterministic design input, not evidence that a venue, hardware path, calibration, runtime recovery, or restore rehearsal has passed.</p><p><a href="{_h(twin_url)}">Inspect the exact digital twin</a> · <a href="{_h(gates_url)}">Inspect the exact gate ledger</a></p></div><aside class="panel"><h3>Physical predicates</h3><p>{len(reference['blocked_gates'])} gates remain blocked in the bound reference ledger; issue 14 cannot close from this simulation.</p><ul>{physical_gates}</ul></aside></div></section>
      <section id="access" aria-labelledby="access-title"><div class="grid"><div><p class="kicker">Accessibility</p><h2 id="access-title">A complete work with or without camera, motion, or sound</h2><p><strong>Visual description.</strong> {_h(accessibility['alt_text'])}</p><p><strong>Motion.</strong> {_h(accessibility['motion_note'])}</p><p><strong>Audio.</strong> {_h(accessibility['audio_note'])}</p></div><aside class="panel"><h3>Fallbacks</h3><p>{_h(accessibility['reduced_motion'])}</p><p>{_h(accessibility['silent_fallback'])}</p><p>Captions: {_h(accessibility['captions']['status'])}. Transcript: {_h(accessibility['transcript']['status'])}.</p></aside></div></section>
      <section id="resources" aria-labelledby="resources-title"><div class="grid"><div><p class="kicker">Resources</p><h2 id="resources-title">Access and presentation downloads</h2><p>These files are generated from the same approved release manifest and authenticated by the release receipt.</p></div><nav class="panel" aria-label="Project resources"><ul class="clean">{''.join(resources)}</ul></nav></div></section>
      <section aria-labelledby="press-title"><div class="grid"><div><p class="kicker">For presentation</p><h2 id="press-title">Synopsis</h2><p>{_h(press['synopsis_long'])}</p></div><aside class="panel"><h3>Canonical links</h3><ul>{links}</ul><h3>Seed sharing</h3><p>{_h(press['seed_sharing']['note'])}</p><p><a href="{_h(press['seed_sharing']['example_url'])}">Open archival seed {press['seed_sharing']['archival_seed']}</a></p></aside></div></section>
      <section id="evidence" aria-labelledby="evidence-title"><p class="kicker">Release truth</p><h2 id="evidence-title">Claims and evidence</h2><ul class="clean">{''.join(claims)}</ul></section>
      <section aria-labelledby="credits-title"><div class="grid"><div><h2 id="credits-title">Credits</h2><ul class="clean">{''.join(credit_items)}</ul></div><div><h2>External release media</h2><ul class="clean">{''.join(media)}</ul><h3>Generated release products</h3><ul class="clean">{''.join(generated_products)}</ul></div></div></section>
      {f'<section aria-labelledby="gates-title"><p class="kicker">Draft gate ledger</p><h2 id="gates-title">What must happen before publication</h2><ul class="clean">{"".join(open_gates)}</ul></section>' if draft else ''}
    </main>
    <footer><p>{_h(identity['canonical_title'])} by {_h(identity['artist'])}. Built from release manifest {_h(manifest['version'])}, source {_h(commit)}. No account action, public send, or deployment is performed by this build.</p></footer>
  </div>
</body>
</html>
"""
    return document.encode("utf-8")


def project_html(
    manifest: dict,
    phase: str,
    commit: str,
    *,
    contract: str | None = None,
) -> bytes:
    """Render one explicitly versioned project-page byte contract."""
    source_contract = manifest["artifact_contracts"]["project_page"]
    if contract is None:
        contract = source_contract
    if contract != source_contract:
        raise ReleaseError(
            "project-page contract does not match the source manifest"
        )
    if contract == PROJECT_PAGE_CONTRACT:
        return _project_html_v1(manifest, phase, commit)
    raise ReleaseError(f"unsupported project-page contract: {contract}")


def accessibility_markdown(manifest: dict, phase: str) -> bytes:
    access = manifest["accessibility"]
    prefix = "# Accessibility materials\n\n"
    if phase == "draft":
        prefix += "> **DRAFT - NOT FOR PUBLICATION.** Final media and human review gates remain open.\n\n"
    body = f"""{prefix}## Visual description

{access['alt_text']}

## Canvas description

{access['canvas_description']}

## Motion and flashing

{access['motion_note']}

## Audio

{access['audio_note']}

## Reduced motion

{access['reduced_motion']}

## Silent and no-camera fallback

{access['silent_fallback']}

## Caption and transcript state

- Captions: {access['captions']['status']} - {access['captions']['reason'] or 'approved content follows the manifest'}
- Transcript: {access['transcript']['status']} - {access['transcript']['reason'] or 'approved content follows the manifest'}
"""
    return body.encode("utf-8")


def captions_vtt(manifest: dict, phase: str) -> bytes:
    captions = manifest["accessibility"]["captions"]
    lines = ["WEBVTT", ""]
    if captions["status"] != "approved":
        lines.extend(
            [
                "NOTE This is a draft caption ledger and is not a public caption track.",
                f"NOTE {captions['reason']}",
                "",
            ]
        )
    else:
        for index, cue in enumerate(captions["cues"], start=1):
            lines.extend([str(index), f"{cue['start']} --> {cue['end']}", cue["text"], ""])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def transcript_text(manifest: dict, phase: str) -> bytes:
    transcript = manifest["accessibility"]["transcript"]
    lines = [manifest["identity"]["canonical_title"], "ACCESSIBLE TRANSCRIPT", ""]
    if phase == "draft":
        lines.extend(["DRAFT - NOT FOR PUBLICATION", ""])
    lines.append(transcript["text"])
    if transcript["reason"]:
        lines.extend(["", f"Status note: {transcript['reason']}"])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def press_markdown(manifest: dict, phase: str) -> bytes:
    press = manifest["press"]
    identity = manifest["identity"]
    draft = "> **DRAFT - NOT FOR PUBLICATION.** Copy, credits, contact, and media remain gated.\n\n" if phase == "draft" else ""
    links = "\n".join(f"- [{item['label']}]({item['url']})" for item in press["canonical_links"])
    media = "\n".join(
        f"- **{item['label']}** ({item['kind']}): {item['status']}; clearance {item['clearance']['status']}"
        for item in manifest["media"]
    )
    content = f"""# {identity['canonical_title']} - press kit

{draft}## Short synopsis

{press['synopsis_short']}

## Long synopsis

{press['synopsis_long']}

## Artist statement

{press['artist_statement']}

## Artist biography

{press['artist_bio']}

## Contact

{press['contact']['label']}: {press['contact']['url'] or 'pending owner approval'}

## Canonical links

{links}

## Seed sharing

{press['seed_sharing']['note']}

Archival seed: {press['seed_sharing']['archival_seed']}

Example: {press['seed_sharing']['example_url']}

## Release media

{media}
"""
    return content.encode("utf-8")


def credits_text(manifest: dict, phase: str) -> bytes:
    lines = [manifest["identity"]["canonical_title"], "CREDITS", ""]
    if phase == "draft":
        lines.extend(["DRAFT - NOT FOR PUBLICATION", ""])
    for credit in manifest["credits"]:
        name = credit["name"] or "WITHHELD PENDING CLEARANCE"
        lines.extend(
            [
                f"{credit['role']}: {name}",
                f"Status: {credit['status']}",
                credit["note"],
                "",
            ]
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def media_inventory(
    manifest: dict,
    phase: str,
    copied: list[dict],
    generated: list[dict],
) -> bytes:
    records = []
    copied_by_id = {record["id"]: record for record in copied}
    for medium in sorted(manifest["media"], key=lambda item: item["id"]):
        source = medium["source"]
        released = copied_by_id.get(medium["id"])
        records.append(
            {
                "id": medium["id"],
                "kind": medium["kind"],
                "label": medium["label"],
                "required_for": medium["required_for"],
                "status": medium["status"],
                "clearance": medium["clearance"]["status"],
                "alt_text": medium["alt_text"],
                "source": (
                    {"path": source["path"], "sha256": source["sha256"]}
                    if source is not None
                    else None
                ),
                "released": released,
            }
        )
    generated_by_id = {record["id"]: record for record in generated}
    products = []
    for product in sorted(manifest["products"], key=lambda item: item["id"]):
        products.append(
            {
                "id": product["id"],
                "kind": product["kind"],
                "label": product["label"],
                "required_for": product["required_for"],
                "status": product["status"],
                "path": product["path"],
                "artifact": generated_by_id[product["id"]],
            }
        )
    return canonical_json(
        {
            "schema": "danse.release-media.v1",
            "release_id": manifest["release_id"],
            "version": manifest["version"],
            "phase": phase,
            "media": records,
            "products": products,
        }
    )


def _pdf_ascii(value: object) -> str:
    return (
        str(value)
        .replace("\u2014", " - ")
        .replace("\u2013", "-")
        .replace("\u2011", "-")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


class PitchPDF:
    """Small deterministic PDF compositor with explicit page geometry."""

    def __init__(self, manifest: dict, phase: str, commit: str) -> None:
        self.manifest = manifest
        self.phase = phase
        self.commit = commit
        self.buffer = io.BytesIO()
        self.width, self.height = LETTER
        self.canvas = canvas.Canvas(
            self.buffer,
            pagesize=LETTER,
            pageCompression=1,
            invariant=1,
        )
        self.canvas.setTitle(_pdf_ascii(manifest["identity"]["canonical_title"]))
        self.canvas.setAuthor(_pdf_ascii(manifest["identity"]["artist"]))
        self.canvas.setSubject("Deterministic installation pitch and release evidence ledger")
        self.canvas.setCreator("Danse deterministic release builder")
        self.canvas.setProducer("Danse deterministic release builder")
        self.page = 0
        self.y = 0.0
        self.margin = 54.0
        self.body_width = self.width - 2 * self.margin
        self.page_capacity = (
            self.height
            - 2 * self.margin
            - 24
            - (42 if self.phase == "draft" else 0)
        )

    def _footer(self) -> None:
        self.canvas.setStrokeColor(colors.HexColor("#c9c2b8"))
        self.canvas.line(self.margin, 38, self.width - self.margin, 38)
        self.canvas.setFillColor(colors.HexColor("#625d57"))
        self.canvas.setFont("Helvetica", 7.5)
        self.canvas.drawString(self.margin, 25, f"Danse release manifest {self.manifest['version']}")
        self.canvas.drawRightString(self.width - self.margin, 25, f"Page {self.page} | {self.commit[:12]}")

    def new_page(self, label: str) -> None:
        if self.page:
            self._footer()
            self.canvas.showPage()
        self.page += 1
        self.y = self.height - self.margin
        self.canvas.setFillColor(colors.HexColor("#ed7745"))
        self.canvas.setFont("Helvetica-Bold", 8)
        self.canvas.drawString(self.margin, self.y, _pdf_ascii(label).upper())
        self.y -= 24
        if self.phase == "draft":
            self.canvas.setFillColor(colors.HexColor("#f4e4da"))
            self.canvas.setStrokeColor(colors.HexColor("#8e351f"))
            self.canvas.rect(self.margin, self.y - 20, self.body_width, 26, fill=1, stroke=1)
            self.canvas.setFillColor(colors.HexColor("#6f2b1b"))
            self.canvas.setFont("Helvetica-Bold", 8)
            self.canvas.drawString(self.margin + 8, self.y - 12, "DRAFT - NOT FOR PUBLICATION - RELEASE GATES REMAIN OPEN")
            self.y -= 42

    def ensure(self, height: float, label: str = "Pitch continued") -> None:
        if height > self.page_capacity:
            raise ReleaseError(
                f"pitch PDF block height {height:g} exceeds page capacity {self.page_capacity:g}"
            )
        if self.y - height < 54:
            self.new_page(label)

    def heading(self, text: str, size: float = 18) -> None:
        self.ensure(size + 24)
        self.canvas.setFillColor(colors.HexColor("#12171c"))
        self.canvas.setFont("Helvetica-Bold", size)
        self.canvas.drawString(self.margin, self.y, _pdf_ascii(text))
        self.y -= size + 10

    def wrapped_lines(
        self,
        text: str,
        *,
        size: float,
        width: float,
        font: str = "Helvetica",
    ) -> list[str]:
        words = _pdf_ascii(text).split()
        lines: list[str] = []
        line = ""
        for word in words:
            if stringWidth(word, font, size) > width:
                raise ReleaseError(f"pitch PDF contains an unbreakable overlong word: {word!r}")
            candidate = f"{line} {word}".strip()
            if line and stringWidth(candidate, font, size) > width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        return lines

    def paragraph(self, text: str, *, size: float = 9.5, leading: float = 13, color: str = "#373b3f") -> None:
        lines = self.wrapped_lines(text, size=size, width=self.body_width)
        block_height = len(lines) * leading + 10
        if block_height <= self.page_capacity:
            self.ensure(block_height)
        for line in lines:
            self.ensure(leading)
            self.canvas.setFillColor(colors.HexColor(color))
            self.canvas.setFont("Helvetica", size)
            self.canvas.drawString(self.margin, self.y, line)
            self.y -= leading
        self.y -= 7

    def bullet(self, label: str, text: str, status: str | None = None) -> None:
        status_text = f" [{status.upper()}]" if status else ""
        self.paragraph(f"{label}{status_text}: {text}", size=9, leading=12)

    def diagram(self, nodes: list[dict]) -> None:
        gap = 12
        details = [
            self.wrapped_lines(
                node["detail"],
                size=7.8,
                width=self.body_width - 24,
            )
            for node in nodes
        ]
        box_heights = [38 + 10 * len(lines) for lines in details]
        total = sum(box_heights) + (len(nodes) - 1) * gap
        if total + 12 <= self.page_capacity:
            self.ensure(total + 12)
        for index, (node, lines, box_height) in enumerate(
            zip(nodes, details, box_heights, strict=True)
        ):
            self.ensure(box_height + gap)
            x = self.margin
            y = self.y - box_height
            self.canvas.setFillColor(colors.HexColor("#f4f0e7"))
            self.canvas.setStrokeColor(colors.HexColor("#ed7745"))
            self.canvas.roundRect(x, y, self.body_width, box_height, 6, fill=1, stroke=1)
            self.canvas.setFillColor(colors.HexColor("#12171c"))
            self.canvas.setFont("Helvetica-Bold", 10)
            self.canvas.drawString(x + 12, y + box_height - 19, _pdf_ascii(node["label"]))
            self.canvas.setFont("Helvetica", 7.8)
            detail_y = y + box_height - 34
            for line in lines:
                self.canvas.drawString(x + 12, detail_y, line)
                detail_y -= 10
            self.y = y - gap
            if index < len(nodes) - 1:
                self.canvas.setStrokeColor(colors.HexColor("#ed7745"))
                self.canvas.line(self.width / 2, y, self.width / 2, y - gap + 2)

    def finish(self) -> bytes:
        self._footer()
        self.canvas.save()
        return self.buffer.getvalue()


def pitch_pdf(manifest: dict, phase: str, commit: str) -> bytes:
    pdf = PitchPDF(manifest, phase, commit)
    identity = manifest["identity"]
    copy = manifest["copy"]
    installation = manifest["installation"]
    access = manifest["accessibility"]
    press = manifest["press"]

    pdf.new_page("Installation pitch")
    pdf.heading(identity["canonical_title"], 25)
    pdf.paragraph(copy["eyebrow"], size=11, leading=15, color="#ed7745")
    pdf.paragraph(copy["logline"], size=14, leading=19, color="#12171c")
    pdf.paragraph(f"Artist: {identity['artist']} | Origin: {identity['origin_year']} | Release: {manifest['version']}")
    pdf.paragraph(copy["concept"])

    pdf.new_page("Concept and system")
    pdf.heading("Installation concept")
    pdf.paragraph(copy["installation_concept"])
    pdf.heading("System flow", 15)
    pdf.diagram(installation["system_flow"])

    pdf.new_page("Space and interaction")
    pdf.heading("Spatial requirements")
    for item in installation["spatial_requirements"]:
        pdf.bullet(item["item"], item["detail"], item["status"])
    pdf.heading("Interaction model", 15)
    for item in installation["interaction_model"]:
        pdf.bullet("Model", item)

    pdf.new_page("Technical rider")
    pdf.heading("Venue-validated technical rider")
    for item in installation["technical_rider"]:
        pdf.bullet(item["item"], item["detail"], item["status"])
    pdf.paragraph("No persistent LaunchAgent is part of the development or venue design. Recovery remains an issue 14 evidence gate.", color="#625d57")
    reference = installation["reference_contract"]
    pdf.heading("Reference installation contract", 15)
    pdf.bullet(
        "Digital twin",
        f"{reference['spec_id']} at contract digest {reference['spec_contract_sha256']}. This is a deterministic reference simulation, not physical evidence.",
        reference["status"],
    )
    pdf.bullet(
        "Physical gate ledger",
        f"{len(reference['blocked_gates'])} venue, hardware, calibration, recovery, and restore predicates remain blocked; issue 14 cannot close.",
        "blocked",
    )

    pdf.new_page("Accessibility and public materials")
    pdf.heading("Accessibility")
    pdf.bullet("Visual description", access["alt_text"])
    pdf.bullet("Motion", access["motion_note"])
    pdf.bullet("Audio", access["audio_note"])
    pdf.bullet("Reduced motion", access["reduced_motion"])
    pdf.bullet("Silent fallback", access["silent_fallback"])
    pdf.bullet("Captions", access["captions"]["reason"] or "Approved", access["captions"]["status"])
    pdf.bullet("Transcript", access["transcript"]["reason"] or "Approved", access["transcript"]["status"])
    pdf.heading("Press synopsis", 15)
    pdf.paragraph(press["synopsis_short"])

    pdf.new_page("Rights, credits, and evidence")
    pdf.heading("Credits and rights state")
    for credit in manifest["credits"]:
        name = credit["name"] or "name withheld pending clearance"
        pdf.bullet(credit["role"], f"{name}. {credit['note']}", credit["status"])
    pdf.heading("Evidence ledger", 15)
    for claim in manifest["claims"]:
        pdf.bullet(claim["id"], claim["text"], claim["status"])
    pdf.new_page("Release gates")
    pdf.heading("Required evidence before publication")
    for gate in manifest["gates"]:
        pdf.bullet(gate["id"], gate["action"], gate["state"])
    return pdf.finish()


def manifested_media_records(manifest: dict, phase: str) -> list[dict]:
    """Derive every copied-media identity directly from the source manifest."""
    if phase not in PHASES:
        raise ReleaseError(f"unknown release payload phase: {phase}")
    records: list[dict] = []
    paths: set[str] = set()
    ids: set[str] = set()
    for medium in manifest["media"]:
        source = medium["source"]
        if (
            phase not in medium["required_for"]
            or medium["status"] != "ready"
            or medium["clearance"]["status"] != "cleared"
            or source is None
        ):
            continue
        destination = safe_relative(
            source["destination"],
            f"media {medium['id']} destination",
        )
        if medium["id"] in ids or destination in paths:
            raise ReleaseError("release media identities or destinations are duplicated")
        ids.add(medium["id"])
        paths.add(destination)
        records.append(
            {
                "id": medium["id"],
                "path": destination,
                "bytes": source["bytes"],
                "sha256": source["sha256"],
            }
        )
    return records


def _generated_release_files_v1(
    manifest: dict,
    phase: str,
    commit: str,
    copied: list[dict],
) -> dict[str, bytes]:
    generated_files = {
        "project/index.html": project_html(
            manifest,
            phase,
            commit,
            contract=manifest["artifact_contracts"]["project_page"],
        ),
        PDF_NAME: pitch_pdf(manifest, phase, commit),
        "accessibility/accessibility.md": accessibility_markdown(manifest, phase),
        "accessibility/captions.en.vtt": captions_vtt(manifest, phase),
        "accessibility/transcript.txt": transcript_text(manifest, phase),
        "press/press-kit.md": press_markdown(manifest, phase),
        "press/credits.txt": credits_text(manifest, phase),
        "press/posting-calendar.json": canonical_json(
            {
                "schema": "danse.release-posting-calendar.v1",
                "release_id": manifest["release_id"],
                "phase": phase,
                "publishes_automatically": False,
                "items": manifest["press"]["posting_calendar"],
            }
        ),
    }
    generated_products = []
    for product in manifest["products"]:
        relative = product["path"]
        if (
            GENERATED_PRODUCT_PATHS.get(product["id"]) != relative
            or relative not in generated_files
        ):
            raise ReleaseError(
                f"generated product {product['id']} has no canonical builder output"
            )
        data = generated_files[relative]
        generated_products.append(
            {
                "id": product["id"],
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    generated_files["media/release-media.json"] = media_inventory(
        manifest,
        phase,
        copied,
        generated_products,
    )
    if set(generated_files) != set(GENERATED_PATHS):
        raise ReleaseError("release builder's generated output contract drifted")
    return generated_files


def generated_release_files(
    manifest: dict,
    phase: str,
    commit: str,
    copied: list[dict],
) -> dict[str, bytes]:
    """Dispatch the source-bound, historically preserved payload renderer."""
    contract = manifest["artifact_contracts"]["release_payload"]
    if contract == RELEASE_PAYLOAD_CONTRACT:
        return _generated_release_files_v1(manifest, phase, commit, copied)
    raise ReleaseError(f"unsupported release-payload contract: {contract}")


def release_payload_records(manifest: dict, phase: str, commit: str) -> dict[str, dict]:
    """Return the complete source-bound identity of one release artifact."""
    copied = manifested_media_records(manifest, phase)
    generated = generated_release_files(manifest, phase, commit, copied)
    records = {
        record["path"]: {
            "path": record["path"],
            "bytes": record["bytes"],
            "sha256": record["sha256"],
        }
        for record in copied
    }
    for relative, data in generated.items():
        if relative in records:
            raise ReleaseError(f"generated output collides with release media: {relative}")
        records[relative] = {
            "path": relative,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return records


def selected_release_records(
    manifest: dict,
    phase: str,
    commit: str,
) -> dict[str, dict]:
    """Select the exact generated/media records admitted to a composed surface."""
    payload = release_payload_records(manifest, phase, commit)
    selected = {
        record["path"]: payload[record["path"]]
        for record in manifested_media_records(manifest, phase)
    }
    for product in manifest["products"]:
        if phase not in product["required_for"]:
            continue
        if product["status"] != "ready":
            raise ReleaseError(
                f"{phase} generated product {product['id']} is not admitted"
            )
        relative = product["path"]
        if relative not in payload or relative in selected:
            raise ReleaseError(
                f"{phase} generated product {product['id']} has an invalid identity"
            )
        selected[relative] = payload[relative]
    return selected


def write_bytes(output: Path, relative: str, data: bytes) -> Path:
    relative = safe_relative(relative, "artifact output path")
    target = output / PurePosixPath(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o644)
    os.utime(target, (0, 0), follow_symlinks=False)
    return target


def copy_manifested_media(
    source_path: Path,
    target: Path,
    record: dict,
    label: str,
) -> dict:
    """Copy one approved source through a stable descriptor and recheck its bytes."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    copied_bytes = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(source_path, flags)
        with os.fdopen(descriptor, "rb") as source_handle:
            if not stat.S_ISREG(os.fstat(source_handle.fileno()).st_mode):
                raise ReleaseError(f"{label} is no longer a regular file")
            with target.open("xb") as target_handle:
                for block in iter(lambda: source_handle.read(1 << 20), b""):
                    digest.update(block)
                    copied_bytes += len(block)
                    target_handle.write(block)
    except FileExistsError as exc:
        raise ReleaseError(f"{label} destination appeared during the build") from exc
    except OSError as exc:
        if target.exists() and not target.is_symlink():
            target.unlink()
        raise ReleaseError(f"cannot copy {label}: {exc}") from exc

    copied_sha256 = digest.hexdigest()
    if copied_bytes != record["bytes"] or copied_sha256 != record["sha256"]:
        target.unlink()
        raise ReleaseError(f"{label} changed after manifest validation")
    if target.stat().st_size != copied_bytes or sha256(target) != copied_sha256:
        target.unlink()
        raise ReleaseError(f"{label} destination changed during the build")
    target.chmod(0o644)
    os.utime(target, (0, 0), follow_symlinks=False)
    return {
        "path": target,
        "bytes": copied_bytes,
        "sha256": copied_sha256,
    }


def write_committed_media(
    data: bytes,
    target: Path,
    record: dict,
    label: str,
) -> dict:
    """Materialize one authenticated source-commit blob without checkout filters."""
    copied_bytes = len(data)
    copied_sha256 = hashlib.sha256(data).hexdigest()
    if copied_bytes != record["bytes"] or copied_sha256 != record["sha256"]:
        raise ReleaseError(f"{label} does not match its source-manifest identity")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as target_handle:
            target_handle.write(data)
    except FileExistsError as exc:
        raise ReleaseError(f"{label} destination appeared during the build") from exc
    except OSError as exc:
        if target.exists() and not target.is_symlink():
            target.unlink()
        raise ReleaseError(f"cannot materialize {label}: {exc}") from exc
    if target.stat().st_size != copied_bytes or sha256(target) != copied_sha256:
        target.unlink()
        raise ReleaseError(f"{label} destination changed during the build")
    target.chmod(0o644)
    os.utime(target, (0, 0), follow_symlinks=False)
    return {
        "path": target,
        "bytes": copied_bytes,
        "sha256": copied_sha256,
    }


def artifact_inventory(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError(f"release artifact root must be a regular directory: {root}")
    files: set[str] = set()
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise ReleaseError(f"release artifact contains a symlinked directory: {path.relative_to(root)}")
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                raise ReleaseError(f"release artifact contains a non-regular file: {relative}")
            files.add(relative)
    return files


def validate_git_source(root: Path, expected_commit: str) -> None:
    """Bind a production CLI build to one clean, exact Git worktree."""
    root = root.absolute().resolve()
    expected_commit = require_commit_object(root, expected_commit)
    reject_git_rewrites(root)
    git_env = provenance_git_env()
    identity = subprocess.run(
        provenance_git_command(root, "rev-parse", "--show-toplevel", "HEAD"),
        capture_output=True,
        text=True,
        check=False,
        env=git_env,
    )
    lines = identity.stdout.splitlines()
    if identity.returncode != 0 or len(lines) != 2:
        detail = identity.stderr.strip() or "source root is not a Git worktree"
        raise ReleaseError(f"cannot authenticate source checkout: {detail}")
    if Path(lines[0]).resolve() != root:
        raise ReleaseError("source root must be the Git worktree top level")
    actual_commit = lines[1].strip().lower()
    if actual_commit != expected_commit:
        raise ReleaseError(
            f"source commit {expected_commit} does not match checkout HEAD {actual_commit}"
        )
    status = subprocess.run(
        provenance_git_command(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ),
        capture_output=True,
        text=True,
        check=False,
        env=git_env,
    )
    if status.returncode != 0:
        raise ReleaseError(f"cannot inspect source checkout: {status.stderr.strip()}")
    if status.stdout:
        raise ReleaseError("source checkout has tracked changes or untracked files")


def verify_project_links(
    output: Path,
    delivered_paths: set[str],
    *,
    require_artwork_root: bool = False,
) -> None:
    """Require every local project link to stay inside the release boundary."""
    project_path = output / "project/index.html"
    parser = _ProjectMarkup()
    try:
        parser.feed(project_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ReleaseError(f"project page cannot be inspected: {exc}") from exc
    for href in parser.hrefs:
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != "https" or not parsed.netloc:
                raise ReleaseError(f"project page has a non-HTTPS external link: {href}")
            continue
        if not parsed.path:
            if parsed.fragment and parsed.fragment not in parser.ids:
                raise ReleaseError(f"project page names a missing fragment: #{parsed.fragment}")
            continue
        decoded = unquote(parsed.path)
        relative = posixpath.normpath(posixpath.join("project", decoded))
        if relative == ".":
            if decoded not in {"..", "../"}:
                raise ReleaseError(f"project page has an unsafe root link: {href}")
            if require_artwork_root and "index.html" not in delivered_paths:
                raise ReleaseError("project page artwork-root link has no delivered index")
            continue  # The artwork root enters only when Pages composes this artifact.
        try:
            relative = safe_relative(relative, f"project-page link {href!r}")
        except ReleaseError as exc:
            raise ReleaseError(f"project page link escapes the release artifact: {href}") from exc
        if relative not in delivered_paths:
            raise ReleaseError(f"project page names a missing internal target: {relative}")
        if parsed.fragment and relative == "project/index.html" and parsed.fragment not in parser.ids:
            raise ReleaseError(f"project page names a missing fragment: #{parsed.fragment}")


def verify_project_security_contract(
    contract: object,
    delivered_records: dict[str, dict],
) -> tuple[str, str | None]:
    """Validate the manifest-derived discovery URLs against receipted bytes."""
    if not isinstance(contract, dict) or set(contract) != {
        "project_contract",
        "canonical_url",
        "social_image",
    }:
        raise ReleaseError("project security binding has an unknown shape")
    if contract["project_contract"] != PROJECT_PAGE_CONTRACT:
        raise ReleaseError("project-page contract is unsupported")
    canonical = contract["canonical_url"]
    if canonical != PROJECT_CANONICAL_URL:
        raise ReleaseError("project canonical URL is not manifest-bound")
    social = contract["social_image"]
    if social is None:
        return canonical, None
    if not isinstance(social, dict) or set(social) != {
        "url",
        "path",
        "bytes",
        "sha256",
    }:
        raise ReleaseError("project social-image binding has an unknown shape")
    path = safe_relative(social["path"], "project social-image path")
    if not path.startswith("media/assets/"):
        raise ReleaseError("project social image is outside the released media boundary")
    if social["url"] != PROJECT_SITE_URL + path:
        raise ReleaseError("project social image URL is not manifest-bound")
    identity = {
        "path": path,
        "bytes": social["bytes"],
        "sha256": social["sha256"],
    }
    if delivered_records.get(path) != identity:
        raise ReleaseError("project social image is not bound by the media receipt")
    return canonical, social["url"]


def verify_project_security(
    project: str,
    contract: object,
    delivered_records: dict[str, dict],
) -> None:
    """Require the generated project page to remain passive and network-closed."""
    canonical_url, social_image_url = verify_project_security_contract(
        contract,
        delivered_records,
    )
    parser = _ProjectMarkup()
    parser.feed(project)
    policies = [
        meta.get("content")
        for meta in parser.metas
        if (meta.get("http-equiv") or "").lower() == "content-security-policy"
    ]
    if policies != [PROJECT_CSP]:
        raise ReleaseError("project page lacks the exact fail-closed content security policy")
    referrers = [
        meta.get("content")
        for meta in parser.metas
        if (meta.get("name") or "").lower() == "referrer"
    ]
    if referrers != ["no-referrer"]:
        raise ReleaseError("project page lacks the exact no-referrer policy")
    if parser.html_attributes != [{"lang": "en"}] or parser.head_attributes != [{}]:
        raise ReleaseError("project page opening document attributes are not exact")
    if parser.active_elements:
        raise ReleaseError(
            "project page contains prohibited active elements: "
            + ", ".join(sorted(parser.active_elements))
        )
    if (
        parser.doctypes != 1
        or parser.html_starts != 1
        or parser.html_ends != 1
        or parser.head_starts != 1
        or parser.head_ends != 1
        or parser.in_head
        or parser.body_starts != 1
        or parser.body_ends != 1
        or parser.in_body
        or parser.structure_errors
    ):
        details = ", ".join(sorted(parser.structure_errors)) or "head count"
        raise ReleaseError(f"project page has malformed head structure: {details}")
    exact_csp = {
        "http-equiv": "Content-Security-Policy",
        "content": PROJECT_CSP,
    }
    exact_referrer = {"name": "referrer", "content": "no-referrer"}
    if (
        len(parser.head_elements) < 2
        or parser.head_elements[0] != ("meta", exact_csp)
        or parser.head_elements[1] != ("meta", exact_referrer)
    ):
        raise ReleaseError(
            "project security metadata must be exact and precede all head markup"
        )
    csp_head_positions = [0]
    loading_positions = [
        index
        for index, (tag, _attrs) in enumerate(parser.head_elements)
        if tag
        in {
            "audio",
            "base",
            "embed",
            "form",
            "iframe",
            "img",
            "link",
            "object",
            "script",
            "source",
            "style",
            "track",
            "video",
        }
    ]
    if loading_positions and csp_head_positions[0] > min(loading_positions):
        raise ReleaseError("project content security policy must precede load-bearing markup")
    unexpected_http_equiv = sorted(
        {
            value
            for meta in parser.metas
            if (value := (meta.get("http-equiv") or "").lower())
            and value != "content-security-policy"
        }
    )
    if unexpected_http_equiv:
        raise ReleaseError(
            "project page contains prohibited HTTP-equivalent metadata: "
            + ", ".join(unexpected_http_equiv)
        )
    canonical_links = [
        link
        for link in parser.link_elements
        if set(link) == {"href", "rel"}
        and (link.get("rel") or "").lower().split() == ["canonical"]
        and link.get("href") == canonical_url
    ]
    if len(parser.link_elements) != 1 or len(canonical_links) != 1:
        raise ReleaseError(
            "project page must contain only its manifest-bound canonical link"
        )
    allowed_names = {
        "viewport",
        "referrer",
        "robots",
        "description",
        "twitter:card",
    }
    allowed_properties = {"og:title", "og:description", "og:type", "og:url"}
    if social_image_url is not None:
        allowed_properties.add("og:image")
    named: dict[str, list[str | None]] = {}
    properties: dict[str, list[str | None]] = {}
    charsets: list[str | None] = []
    for meta in parser.metas:
        if set(meta) == {"charset"}:
            charsets.append(meta["charset"])
            continue
        if "http-equiv" in meta:
            if meta != exact_csp:
                raise ReleaseError("project page contains non-canonical HTTP metadata")
            continue
        if set(meta) == {"name", "content"}:
            name = (meta.get("name") or "").lower()
            if name not in allowed_names:
                raise ReleaseError(f"project page contains prohibited named metadata: {name}")
            named.setdefault(name, []).append(meta.get("content"))
            continue
        if set(meta) == {"property", "content"}:
            prop = (meta.get("property") or "").lower()
            if prop not in allowed_properties:
                raise ReleaseError(f"project page contains prohibited property metadata: {prop}")
            properties.setdefault(prop, []).append(meta.get("content"))
            continue
        raise ReleaseError("project page contains metadata outside the generated contract")
    if charsets != ["utf-8"]:
        raise ReleaseError("project page charset metadata drifted")
    if set(named) != allowed_names or any(len(values) != 1 for values in named.values()):
        raise ReleaseError("project page named metadata inventory drifted")
    if named["twitter:card"] != [
        "summary_large_image" if social_image_url is not None else "summary"
    ]:
        raise ReleaseError("project social-card metadata drifted")
    if set(properties) != allowed_properties or any(
        len(values) != 1 for values in properties.values()
    ):
        raise ReleaseError("project page property metadata inventory drifted")
    if properties["og:type"] != ["website"] or properties["og:url"] != [canonical_url]:
        raise ReleaseError("project Open Graph identity is not manifest-bound")
    if social_image_url is not None and properties["og:image"] != [social_image_url]:
        raise ReleaseError("project social image metadata is not receipt-bound")
    if parser.event_handlers:
        raise ReleaseError(
            "project page contains prohibited inline event handlers: "
            + ", ".join(sorted(parser.event_handlers))
        )
    if parser.referrer_policy_overrides:
        raise ReleaseError(
            "project page contains referrer-policy overrides: "
            + ", ".join(sorted(parser.referrer_policy_overrides))
        )
    if parser.duplicate_attributes:
        raise ReleaseError(
            "project page contains duplicate attributes: "
            + ", ".join(sorted(parser.duplicate_attributes))
        )


def _verify_pdf(path: Path, phase: str, title: str) -> None:
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise ReleaseError(f"pitch PDF cannot be reopened: {exc}") from exc
    if reader.is_encrypted or len(reader.pages) < 5:
        raise ReleaseError("pitch PDF must be unencrypted and contain at least five pages")
    if (reader.metadata or {}).get("/Title") != title:
        raise ReleaseError("pitch PDF title metadata drifted")
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    for required in (title, "System flow", "Spatial requirements", "Technical rider", "Accessibility", "Evidence ledger"):
        if required.lower() not in extracted.lower():
            raise ReleaseError(f"pitch PDF is missing required section {required!r}")
    has_draft = "DRAFT - NOT FOR PUBLICATION" in extracted
    if has_draft != (phase == "draft"):
        raise ReleaseError("pitch PDF draft watermark does not match its phase")


def _read_utf8_artifact(output: Path, relative: str, label: str) -> str:
    try:
        return (output / PurePosixPath(relative)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseError(f"{label} is not readable UTF-8: {exc}") from exc


def source_release_manifest(
    root: Path,
    commit: str,
    *,
    allow_worktree_fallback: bool = False,
) -> tuple[dict, str]:
    """Read the release manifest from the declared commit, or an explicit fixture."""
    root = root.absolute().resolve()
    if allow_worktree_fallback and not (root / ".git").exists():
        path = source_file(root, MANIFEST.as_posix(), "release manifest")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ReleaseError(f"release manifest cannot be read: {exc}") from exc
        manifest = decode_json_object(data, "source-commit release manifest")
        return manifest, hashlib.sha256(data).hexdigest()
    if not allow_worktree_fallback:
        commit = require_commit_object(root, commit)
    else:
        reject_git_rewrites(root)
    committed = subprocess.run(
        provenance_git_command(root, "show", f"{commit}:{MANIFEST.as_posix()}"),
        capture_output=True,
        check=False,
        env=provenance_git_env(),
    )
    if committed.returncode == 0:
        data = committed.stdout
    elif allow_worktree_fallback:
        path = source_file(root, MANIFEST.as_posix(), "release manifest")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ReleaseError(f"release manifest cannot be read: {exc}") from exc
    else:
        detail = committed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseError(
            f"cannot resolve release manifest at source commit {commit}: {detail}"
        )
    manifest = decode_json_object(data, "source-commit release manifest")
    return manifest, hashlib.sha256(data).hexdigest()


def active_toolchain() -> dict[str, str]:
    """Return the exact dependency versions that determine generated bytes."""
    return {
        "python": platform.python_version(),
        "pypdf": pypdf.__version__,
        "reportlab": reportlab.Version,
    }


def release_phase_blockers(manifest: dict, phase: str) -> list[str]:
    """Expose the trusted phase predicate after committed-data validation."""
    return phase_blockers(manifest, phase)


def _manifest_evidence_paths(manifest: dict) -> set[str]:
    """Collect every manifest record that participates in phase admission."""
    paths: set[str] = set()

    def include(record: object, label: str) -> None:
        if record is None:
            return
        if not isinstance(record, dict):
            raise ReleaseError(f"{label} must be a record or null")
        paths.add(safe_relative(record.get("path"), f"{label} path"))

    for claim in manifest["claims"]:
        include(claim["evidence"], f"claim {claim['id']} evidence")
    for credit in manifest["credits"]:
        include(credit["evidence"], f"credit {credit['id']} evidence")
    for medium in manifest["media"]:
        include(medium["source"], f"media {medium['id']} source")
        include(
            medium["clearance"]["evidence"],
            f"media {medium['id']} clearance",
        )
    for gate in manifest["gates"]:
        include(gate["evidence"], f"gate {gate['id']} evidence")

    installation = manifest["installation"]["reference_contract"]
    include(installation["digital_twin"], "installation digital twin")
    include(installation["gate_ledger"], "installation gate ledger")
    opportunity = manifest["opportunity_snapshot"]
    paths.update(
        {
            safe_relative(opportunity["path"], "opportunity snapshot path"),
            safe_relative(opportunity["receipt_path"], "opportunity receipt path"),
            safe_relative(
                opportunity["source_evidence_path"],
                "opportunity source-evidence path",
            ),
        }
    )
    return paths


def validate_source_commit_release(
    root: Path,
    commit: str,
    manifest: dict,
    phase: str,
) -> dict:
    """Validate phase eligibility entirely from regular blobs in ``commit``."""
    root = root.absolute().resolve()
    commit = require_commit_object(root, commit)
    with tempfile.TemporaryDirectory(prefix="danse-source-release-") as temporary:
        snapshot = Path(temporary)
        listing = subprocess.run(
            provenance_git_command(
                root,
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                commit,
            ),
            capture_output=True,
            check=False,
            env=provenance_git_env(),
        )
        if listing.returncode != 0:
            raise ReleaseError("cannot enumerate the declared source commit")
        for row in (item for item in listing.stdout.split(b"\0") if item):
            if b"\t" not in row:
                raise ReleaseError("declared source commit has an invalid tree record")
            identity, encoded_path = row.split(b"\t", 1)
            try:
                mode, kind, object_id = identity.decode("ascii").split()
                relative = encoded_path.decode("utf-8")
            except (UnicodeError, ValueError) as exc:
                raise ReleaseError(
                    "declared source commit has an invalid tree identity"
                ) from exc
            if kind != "blob" or mode not in {"100644", "100755"}:
                continue
            relative = safe_relative(relative, "declared source path")
            blob = subprocess.run(
                provenance_git_command(root, "cat-file", "blob", object_id),
                capture_output=True,
                check=False,
                env=provenance_git_env(),
            )
            if blob.returncode != 0:
                raise ReleaseError(
                    f"cannot read declared source file {relative}"
                )
            target = snapshot / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)
            target.chmod(0o755 if mode == "100755" else 0o644)

        validate_schema(snapshot, manifest)

        # The declared commit supplies data only. Executable validators always
        # come from this verifier's trusted, versioned source tree.
        validated = validate_release(
            snapshot,
            phase=phase,
            checker_root=ROOT,
            provenance_root=root,
            provenance_commit=commit,
        )
        if validated != manifest:
            raise ReleaseError(
                "source-commit release validation returned different manifest bytes"
            )
        return validated


def verify_artifact(
    output: Path,
    expected_commit: str | None = None,
    *,
    source_root: Path = ROOT,
    allow_worktree_manifest: bool = False,
) -> dict:
    output = output.absolute()
    if output.is_symlink() or not output.is_dir():
        raise ReleaseError(f"release artifact root must be a regular directory: {output}")
    output = output.resolve()
    receipt_path = output / ARTIFACT_MANIFEST
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ReleaseError(f"release artifact is missing {ARTIFACT_MANIFEST}")
    receipt = load_json(receipt_path, "release artifact receipt")
    if set(receipt) != {"schema", "phase", "source", "toolchain", "release", "files"} or receipt.get("schema") != ARTIFACT_SCHEMA:
        raise ReleaseError("release artifact receipt has an unknown shape or schema")
    if receipt["phase"] not in PHASES:
        raise ReleaseError("release artifact receipt has an unknown phase")
    source = receipt["source"]
    if set(source) != {"repository", "commit"} or source["repository"] != REPOSITORY:
        raise ReleaseError("release artifact source receipt is invalid")
    commit = source_commit(output, source["commit"])
    if expected_commit is not None and commit != source_commit(output, expected_commit):
        raise ReleaseError(f"release artifact commit {commit} does not match expected {expected_commit}")
    toolchain = receipt["toolchain"]
    if not isinstance(toolchain, dict) or set(toolchain) != {"python", "pypdf", "reportlab"}:
        raise ReleaseError("release artifact toolchain receipt is invalid")
    if not all(isinstance(value, str) and value for value in toolchain.values()):
        raise ReleaseError("release artifact toolchain versions must be non-empty strings")
    expected_toolchain = active_toolchain()
    if toolchain != expected_toolchain:
        raise ReleaseError(
            "release artifact requires its exact receipted generation toolchain; "
            f"receipt={toolchain}, active={expected_toolchain}"
        )
    release = receipt["release"]
    if set(release) != {
        "id",
        "version",
        "payload_contract",
        "manifest",
        "project_security",
        "installation_reference",
        "opportunity_snapshot",
        "opportunity_receipt",
        "source_evidence",
    }:
        raise ReleaseError("release artifact identity receipt is invalid")
    for key in ("manifest", "opportunity_receipt", "source_evidence"):
        record = release[key]
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise ReleaseError(f"release artifact {key} binding is invalid")
        if not HEX64.fullmatch(str(record["sha256"])):
            raise ReleaseError(f"release artifact {key} digest is invalid")
    snapshot_record = release["opportunity_snapshot"]
    if not isinstance(snapshot_record, dict) or set(snapshot_record) != {"path", "sha256", "frozen_at"}:
        raise ReleaseError("release artifact opportunity_snapshot binding is invalid")
    if not HEX64.fullmatch(str(snapshot_record["sha256"])):
        raise ReleaseError("release artifact opportunity_snapshot digest is invalid")
    if release["manifest"]["path"] != MANIFEST.as_posix():
        raise ReleaseError("release artifact points at a non-canonical release manifest")
    source_manifest, source_manifest_sha256 = source_release_manifest(
        source_root,
        commit,
        allow_worktree_fallback=allow_worktree_manifest,
    )
    if release["manifest"]["sha256"] != source_manifest_sha256:
        raise ReleaseError("release artifact manifest digest does not match its source commit")
    try:
        if allow_worktree_manifest:
            validated_source_manifest = validate_release(
                source_root,
                phase=receipt["phase"],
            )
        else:
            validated_source_manifest = validate_source_commit_release(
                source_root,
                commit,
                source_manifest,
                receipt["phase"],
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError(
            f"source-commit release manifest failed schema/evidence validation: {exc}"
        ) from exc
    if validated_source_manifest != source_manifest:
        raise ReleaseError(
            "validated source-commit release manifest drifted from committed bytes"
        )
    if (
        release["id"] != source_manifest["release_id"]
        or release["version"] != source_manifest["version"]
        or release["payload_contract"]
        != source_manifest["artifact_contracts"]["release_payload"]
        or release["payload_contract"] != RELEASE_PAYLOAD_CONTRACT
    ):
        raise ReleaseError("release artifact identity drifted from its source manifest")
    expected_project_security = project_security_contract(
        source_manifest,
        receipt["phase"],
    )
    if release["project_security"] != expected_project_security:
        raise ReleaseError(
            "release artifact project security binding drifted from its source manifest"
        )
    installation_reference = release["installation_reference"]
    if not isinstance(installation_reference, dict) or set(installation_reference) != {
        "schema",
        "status",
        "spec_id",
        "spec_contract_sha256",
        "digital_twin",
        "gate_ledger",
        "physical_predicates_satisfied",
        "issue_14_can_close",
        "blocked_gates",
    }:
        raise ReleaseError("release artifact installation reference binding is invalid")
    if (
        installation_reference["schema"] != "danse.installation.reference-binding.v1"
        or installation_reference["status"] != "reference-only"
        or not HEX64.fullmatch(str(installation_reference["spec_contract_sha256"]))
        or installation_reference["physical_predicates_satisfied"] is not False
        or installation_reference["issue_14_can_close"] is not False
        or not isinstance(installation_reference["blocked_gates"], list)
        or len(installation_reference["blocked_gates"]) != 8
        or len(set(installation_reference["blocked_gates"])) != 8
    ):
        raise ReleaseError("release artifact installation reference state is invalid")
    for key, expected_path in (
        ("digital_twin", "installation/digital-twin.json"),
        ("gate_ledger", "installation/gates.json"),
    ):
        record = installation_reference[key]
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256", "bytes"}
            or record["path"] != expected_path
            or not HEX64.fullmatch(str(record["sha256"]))
            or type(record["bytes"]) is not int
            or record["bytes"] < 0
        ):
            raise ReleaseError(f"release artifact installation {key} binding is invalid")
    if installation_reference != source_manifest["installation"]["reference_contract"]:
        raise ReleaseError(
            "release artifact installation binding drifted from its source manifest"
        )
    if release["opportunity_snapshot"]["path"] != "opportunities/omega-20260829.json":
        raise ReleaseError("release artifact points at a non-canonical opportunity snapshot")
    if release["opportunity_receipt"]["path"] != "opportunities/omega-20260829.receipt.json":
        raise ReleaseError("release artifact points at a non-canonical opportunity receipt")
    if release["source_evidence"]["path"] != "opportunities/source-evidence-20260826.json":
        raise ReleaseError("release artifact points at a non-canonical source-evidence manifest")
    if (
        release["opportunity_snapshot"]["sha256"] != EXPECTED_OPPORTUNITY_SHA256
        or release["opportunity_snapshot"]["frozen_at"] != EXPECTED_OPPORTUNITY_FROZEN_AT
        or release["opportunity_receipt"]["sha256"] != EXPECTED_OPPORTUNITY_RECEIPT_SHA256
        or release["source_evidence"]["sha256"] != EXPECTED_SOURCE_EVIDENCE_SHA256
    ):
        raise ReleaseError("release artifact frozen-registry binding drifted")

    records = receipt["files"]
    if not isinstance(records, list):
        raise ReleaseError("release artifact file inventory must be a list")
    paths: list[str] = []
    delivered_records: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
            raise ReleaseError("release artifact contains a malformed file record")
        relative = safe_relative(record["path"], "release artifact file")
        if relative == ARTIFACT_MANIFEST:
            raise ReleaseError("release artifact receipt cannot digest itself")
        path = output / PurePosixPath(relative)
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"release artifact names a missing or non-regular file: {relative}")
        if not isinstance(record["bytes"], int) or record["bytes"] < 0:
            raise ReleaseError(f"release artifact has an invalid byte count for {relative}")
        if not HEX64.fullmatch(str(record["sha256"])):
            raise ReleaseError(f"release artifact has an invalid digest for {relative}")
        if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            raise ReleaseError(f"release artifact digest mismatch: {relative}")
        paths.append(relative)
        delivered_records[relative] = {
            "path": relative,
            "bytes": record["bytes"],
            "sha256": record["sha256"],
        }
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ReleaseError("release artifact paths must be sorted and unique")
    inventory = artifact_inventory(output)
    expected = set(paths) | {ARTIFACT_MANIFEST}
    if inventory != expected:
        raise ReleaseError(
            f"release artifact inventory mismatch; extra={sorted(inventory - expected)}, missing={sorted(expected - inventory)}"
        )

    required_outputs = {"project/index.html", "accessibility/captions.en.vtt", PDF_NAME}
    missing_outputs = sorted(required_outputs - set(paths))
    if missing_outputs:
        raise ReleaseError(f"release artifact receipt omits required outputs: {missing_outputs}")

    project = _read_utf8_artifact(output, "project/index.html", "project page")
    verify_project_links(output, set(paths))
    verify_project_security(
        project,
        expected_project_security,
        delivered_records,
    )
    expected_project = project_html(
        source_manifest,
        receipt["phase"],
        commit,
        contract=expected_project_security["project_contract"],
    ).decode("utf-8")
    if project != expected_project:
        raise ReleaseError(
            "project page does not reproduce the source-manifest public claims"
        )
    draft = receipt["phase"] == "draft"
    if ('name="robots" content="noindex,nofollow"' in project) != draft:
        raise ReleaseError("project-page robot policy does not match artifact phase")
    if ("Draft - not for publication" in project) != draft:
        raise ReleaseError("project-page draft banner does not match artifact phase")
    if "@media (prefers-reduced-motion:reduce)" not in project:
        raise ReleaseError("project page lacks reduced-motion handling")
    if (
        installation_reference["spec_id"] not in project
        or installation_reference["spec_contract_sha256"] not in project
    ):
        raise ReleaseError("project page does not expose its installation reference binding")
    captions = _read_utf8_artifact(
        output,
        "accessibility/captions.en.vtt",
        "caption artifact",
    )
    if not captions.startswith("WEBVTT\n"):
        raise ReleaseError("caption artifact is not WebVTT")
    _verify_pdf(output / PDF_NAME, receipt["phase"], project_title(project))
    expected_payload = release_payload_records(
        source_manifest,
        receipt["phase"],
        commit,
    )
    if delivered_records != expected_payload:
        extra = sorted(set(delivered_records) - set(expected_payload))
        missing = sorted(set(expected_payload) - set(delivered_records))
        changed = sorted(
            path
            for path in set(delivered_records) & set(expected_payload)
            if delivered_records[path] != expected_payload[path]
        )
        raise ReleaseError(
            "release payload does not reproduce its source-manifest contract; "
            f"extra={extra}, missing={missing}, changed={changed}"
        )
    return receipt


def project_title(project: str) -> str:
    match = re.search(r"<h1>([^<]+)</h1>", project)
    if not match:
        raise ReleaseError("project page has no canonical title heading")
    return html.unescape(match.group(1))


def build(
    root: Path,
    output: Path,
    phase: str,
    commit: str,
    *,
    require_git_source: bool = False,
) -> dict:
    root = root.absolute()
    commit = source_commit(root, commit)
    if require_git_source:
        validate_git_source(root, commit)
    source_manifest, source_manifest_sha256 = source_release_manifest(
        root,
        commit,
        allow_worktree_fallback=not require_git_source,
    )
    if require_git_source:
        manifest = validate_source_commit_release(
            root,
            commit,
            source_manifest,
            phase,
        )
    else:
        manifest = validate_release(root, phase=phase)
        if source_manifest != manifest:
            raise ReleaseError(
                "validated release manifest does not match the declared source commit"
            )
    output = output.absolute()
    if output.is_symlink():
        raise ReleaseError(f"refusing symlinked release output: {output}")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ReleaseError(f"release output must be absent or empty: {output}")
    output_resolved = output.resolve()
    root_resolved = root.resolve()
    if output_resolved == root_resolved or root_resolved in output_resolved.parents:
        raise ReleaseError("release output must be outside the source repository")
    output.mkdir(parents=True, exist_ok=True)

    copied: list[dict] = []
    for medium in manifest["media"]:
        source = medium["source"]
        if (
            phase not in medium["required_for"]
            or medium["status"] != "ready"
            or medium["clearance"]["status"] != "cleared"
            or source is None
        ):
            continue
        label = f"media {medium['id']} source"
        destination = safe_relative(source["destination"], f"media {medium['id']} destination")
        target = output / PurePosixPath(destination)
        if require_git_source:
            data, _executable = source_commit_blob(
                root,
                commit,
                source["path"],
                label,
            )
            copied_record = write_committed_media(data, target, source, label)
        else:
            source_path = source_file(root, source["path"], label)
            copied_record = copy_manifested_media(
                source_path,
                target,
                source,
                label,
            )
        copied.append(
            {
                "id": medium["id"],
                "path": destination,
                "bytes": copied_record["bytes"],
                "sha256": copied_record["sha256"],
            }
        )

    expected_copied = manifested_media_records(manifest, phase)
    if copied != expected_copied:
        raise ReleaseError("copied media drifted from the source-manifest contract")
    generated_files = generated_release_files(manifest, phase, commit, copied)
    for relative, data in generated_files.items():
        write_bytes(output, relative, data)

    files = []
    for relative in sorted(artifact_inventory(output)):
        path = output / PurePosixPath(relative)
        files.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)})
    installation_reference = manifest["installation"]["reference_contract"]
    receipt = {
        "schema": ARTIFACT_SCHEMA,
        "phase": phase,
        "source": {"repository": REPOSITORY, "commit": commit},
        "toolchain": active_toolchain(),
        "release": {
            "id": manifest["release_id"],
            "version": manifest["version"],
            "payload_contract": manifest["artifact_contracts"]["release_payload"],
            "manifest": {
                "path": MANIFEST.as_posix(),
                "sha256": source_manifest_sha256,
            },
            "project_security": project_security_contract(manifest, phase),
            "installation_reference": {
                "schema": installation_reference["schema"],
                "status": installation_reference["status"],
                "spec_id": installation_reference["spec_id"],
                "spec_contract_sha256": installation_reference["spec_contract_sha256"],
                "digital_twin": dict(installation_reference["digital_twin"]),
                "gate_ledger": dict(installation_reference["gate_ledger"]),
                "physical_predicates_satisfied": installation_reference[
                    "physical_predicates_satisfied"
                ],
                "issue_14_can_close": installation_reference["issue_14_can_close"],
                "blocked_gates": list(installation_reference["blocked_gates"]),
            },
            "opportunity_snapshot": {
                "path": manifest["opportunity_snapshot"]["path"],
                "sha256": manifest["opportunity_snapshot"]["sha256"],
                "frozen_at": manifest["opportunity_snapshot"]["frozen_at"],
            },
            "opportunity_receipt": {
                "path": manifest["opportunity_snapshot"]["receipt_path"],
                "sha256": manifest["opportunity_snapshot"]["receipt_sha256"],
            },
            "source_evidence": {
                "path": manifest["opportunity_snapshot"]["source_evidence_path"],
                "sha256": manifest["opportunity_snapshot"]["source_evidence_sha256"],
            },
        },
        "files": files,
    }
    if require_git_source:
        validate_git_source(root, commit)
    write_bytes(output, ARTIFACT_MANIFEST, canonical_json(receipt))
    return verify_artifact(
        output,
        commit,
        source_root=root,
        allow_worktree_manifest=not require_git_source,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="build a new release artifact outside the repository")
    action.add_argument("--verify", type=Path, help="verify an existing release artifact")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--phase", choices=PHASES, default="draft")
    parser.add_argument("--source-commit", help="expected full source commit SHA")
    args = parser.parse_args()
    try:
        if args.output:
            receipt = build(
                args.root,
                args.output,
                args.phase,
                source_commit(args.root, args.source_commit),
                require_git_source=True,
            )
        else:
            receipt = verify_artifact(
                args.verify,
                args.source_commit,
                source_root=args.root,
                allow_worktree_manifest=False,
            )
            if receipt["phase"] != args.phase:
                raise ReleaseError(
                    f"artifact phase {receipt['phase']} does not match expected {args.phase}"
                )
    except ReleaseError as exc:
        parser.exit(1, f"release artifact: FAIL - {exc}\n")
    print(
        f"release artifact: {receipt['release']['id']} {receipt['release']['version']} "
        f"{receipt['phase']} - {len(receipt['files'])} files from {receipt['source']['commit']} verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
