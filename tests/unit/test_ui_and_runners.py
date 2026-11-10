"""
Automated Verification Test Suite for Milestone 4 UI Templates, Execution Runners,
Container Configurations, and Academic Documentation.
"""

import os
import sys
import yaml
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_jinja2_templates_parse_cleanly():
    """Validates that all deployed Jinja2 templates parse without syntax errors."""
    templates_dir = PROJECT_ROOT / "app" / "templates"
    templates = [
        "base.html",
        "index.html",
        "tracking.html",
        "inventory.html",
        "network.html"
    ]
    env = Environment(loader=FileSystemLoader(str(templates_dir)))

    for tmpl_name in templates:
        tmpl_path = templates_dir / tmpl_name
        assert tmpl_path.exists(), f"Template missing: {tmpl_name}"
        # Parse template
        with open(tmpl_path, "r", encoding="utf-8") as f:
            content = f.read()
        parsed = env.parse(content)
        assert parsed is not None, f"Failed to parse {tmpl_name}"


def test_static_assets_contain_critical_definitions():
    """Validates that CSS and JS assets are complete and self-contained."""
    css_path = PROJECT_ROOT / "app" / "static" / "css" / "style.css"
    js_path = PROJECT_ROOT / "app" / "static" / "js" / "dashboard.js"

    assert css_path.exists(), "app/static/css/style.css is missing"
    assert js_path.exists(), "app/static/js/dashboard.js is missing"

    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()
    assert ":root" in css
    assert ".kpi-grid" in css
    assert ".stepper-track" in css
    assert ".data-table" in css
    assert ".topology-svg-container" in css

    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()
    assert "window.IMS" in js
    assert "refreshDashboardKPIs" in js
    assert "lookupShipment" in js
    assert "filterInventoryTable" in js
    assert "submitRestock" in js
    assert "computeRoute" in js
    assert "loadBottlenecks" in js


def test_execution_runners_syntax_and_structure():
    """Validates presence and key execution directives in PowerShell and Bash runners."""
    run_ps1 = PROJECT_ROOT / "run.ps1"
    run_sh = PROJECT_ROOT / "run.sh"
    test_ps1 = PROJECT_ROOT / "test.ps1"
    test_sh = PROJECT_ROOT / "test.sh"

    for script in [run_ps1, run_sh, test_ps1, test_sh]:
        assert script.exists(), f"Script missing: {script.name}"

    with open(run_ps1, "r", encoding="utf-8") as f:
        ps1 = f.read()
    assert "app.db.init_db" in ps1
    assert "app.main" in ps1
    assert "FLASK_APP" in ps1

    with open(run_sh, "r", encoding="utf-8") as f:
        sh = f.read()
    assert "app.db.init_db" in sh
    assert "app.main" in sh

    with open(test_ps1, "r", encoding="utf-8") as f:
        t_ps1 = f.read()
    assert "pytest" in t_ps1
    assert "$LASTEXITCODE" in t_ps1

    with open(test_sh, "r", encoding="utf-8") as f:
        t_sh = f.read()
    assert "pytest" in t_sh
    assert "TEST_EXIT" in t_sh


def test_docker_configurations():
    """Validates Dockerfile instructions and docker-compose YAML validity."""
    dockerfile = PROJECT_ROOT / "Dockerfile"
    compose = PROJECT_ROOT / "docker-compose.yml"

    assert dockerfile.exists(), "Dockerfile missing"
    assert compose.exists(), "docker-compose.yml missing"

    with open(dockerfile, "r", encoding="utf-8") as f:
        df = f.read()
    assert "FROM python:" in df
    assert "COPY requirements.txt" in df
    assert "HEALTHCHECK" in df
    assert "EXPOSE 5000" in df

    with open(compose, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)
    assert "services" in compose_data
    assert "logistics-ims" in compose_data["services"]
    service = compose_data["services"]["logistics-ims"]
    assert "ports" in service
    assert "environment" in service
    assert "healthcheck" in service


def test_readme_academic_coverage():
    """Validates comprehensive academic sections in README.md."""
    readme = PROJECT_ROOT / "README.md"
    assert readme.exists(), "README.md missing"

    with open(readme, "r", encoding="utf-8") as f:
        md = f.read()

    required_keywords = [
        "Relational Storage Engine",
        "NoSQL Document Store",
        "Graph Database & Network Routing Engine",
        "High-Throughput Caching Layer",
        "ACID Transaction Manager",
        "Cryptographic Security & 5-Role RBAC",
        "Quick Start & Execution Guide",
        "run.ps1",
        "run.sh",
        "docker-compose",
        "REST API Reference",
        "Automated Verification & Test Suite",
        "Dijkstra",
        "Haversine",
        "Betweenness Centrality",
        "Cache-Aside"
    ]

    for kw in required_keywords:
        assert kw in md, f"README.md missing key section or keyword: '{kw}'"


if __name__ == "__main__":
    test_jinja2_templates_parse_cleanly()
    print("PASS: test_jinja2_templates_parse_cleanly")
    test_static_assets_contain_critical_definitions()
    print("PASS: test_static_assets_contain_critical_definitions")
    test_execution_runners_syntax_and_structure()
    print("PASS: test_execution_runners_syntax_and_structure")
    test_docker_configurations()
    print("PASS: test_docker_configurations")
    test_readme_academic_coverage()
    print("PASS: test_readme_academic_coverage")
    print("ALL VERIFICATION CHECKS PASSED!")
