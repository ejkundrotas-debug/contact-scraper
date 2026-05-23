from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRED_FILES = [
    "app.py",
    "requirements.txt",
    ".env.example",
    "README.md",
    "modules/__init__.py",
    "modules/schemas.py",
    "modules/router.py",
    "modules/scraper.py",
    "modules/discovery.py",
    "modules/storage.py",
    "modules/pipeline.py",
    "modules/prompts.py",
    "modules/dadata.py",
    "modules/gsheets.py",
    "modules/crm.py",
    "configs/config_default.yaml",
    "configs/config_ru_first.yaml",
    "configs/free-models.yaml",
]

REQUIRED_CAPABILITIES = {"validate", "extract", "okved", "score"}


def main() -> int:
    missing = [p for p in REQUIRED_FILES if not (ROOT / p).exists()]
    if missing:
        raise SystemExit(f"Missing files: {missing}")

    for cfg in ["configs/config_default.yaml", "configs/config_ru_first.yaml"]:
        data = yaml.safe_load((ROOT / cfg).read_text(encoding="utf-8"))
        caps = set()
        for p in data.get("providers", []):
            caps.update(p.get("capabilities", []))
        missed = REQUIRED_CAPABILITIES - caps
        if missed:
            raise SystemExit(f"{cfg} misses capabilities: {missed}")

    # Import package modules. app.py is Streamlit entrypoint and is intentionally not imported here.
    for mod in [
        "modules.schemas",
        "modules.prompts",
        "modules.router",
        "modules.scraper",
        "modules.discovery",
        "modules.storage",
        "modules.pipeline",
        "modules.dadata",
        "modules.gsheets",
        "modules.crm",
    ]:
        importlib.import_module(mod)

    from modules.router import MultiProviderRouter

    router = MultiProviderRouter(ROOT / "configs/config_default.yaml")
    parsed = router._validate_with_schema('```json\n{"a": 1}\n```')
    assert parsed == {"a": 1}
    parsed = router._validate_with_schema('text before {"b": [1,2]} text after')
    assert parsed == {"b": [1, 2]}

    # Phone/email normalization. Порог повышен до 10 цифр — старые тесты (1234567) уже неактуальны.
    from modules.schemas import ContactExtraction

    ce = ContactExtraction(
        phones=["000-00-00", "9999999", "8 (977) 484-74-68", "+7 (977) 484-74-68"],
        emails=["bad", "Info@Example.RU", "info@real-shop.ru"],
    )
    assert "+79774847468" in ce.phones, f"phones={ce.phones}"
    assert "0000000" not in ce.phones
    # example.ru теперь в blacklist
    assert "info@example.ru" not in ce.emails
    assert "info@real-shop.ru" in ce.emails

    # SSRF guard
    from modules.scraper import PublicScraper

    s = PublicScraper(respect_robots=False)
    assert s._is_ssrf_target("http://127.0.0.1:6379/") is True
    assert s._is_ssrf_target("https://example.com/") is False

    # ИНН/ОГРН
    from modules.schemas import INN_RE, OGRN_RE

    assert INN_RE.search("ИНН 7707083893") is not None
    assert OGRN_RE.search("ОГРН 1027700132195") is not None

    output = {
        "ok": True,
        "providers": len(router.providers),
        "capabilities": sorted(REQUIRED_CAPABILITIES),
        "version": "1.2.0",
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
