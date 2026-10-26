"""Командная строка `dms` (план §4). Подкоманды волны 0: kit, config-check, serve; остальные — по волнам."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import CONFIG_FILES, kit_dir, load_yaml


def _cmd_kit(args: argparse.Namespace) -> int:
    """Опись комплекта: ГК → договоры → документы с типами. Ничего не разбирает и не пишет."""
    from .kit import scan, to_dict

    groups = scan()
    if not groups:
        print(f"Комплект не найден: {kit_dir()} (положите его в data/raw/ или задайте DMS_DATA_DIR)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(to_dict(groups), ensure_ascii=False, indent=2))
        return 0
    n = 0
    for g in groups:
        print(f"{g.name} ({g.id}): договоров {len(g.contracts)}")
        for f in g.files:
            print(f"  [общий для ГК] {f.doc_type:<9} {f.path}")
        for c in g.contracts:
            n += 1
            types = ", ".join(f.doc_type for f in c.files)
            print(f"  {c.id} {c.legal_entity}: {types}")
    print(f"Всего договоров: {n}")
    return 0


def _cmd_config_check(_: argparse.Namespace) -> int:
    for name in CONFIG_FILES:
        load_yaml(name)
        print(f"ok  config/{name}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    # access_log=False — журнал uvicorn выключен, сервис ведёт свой (без строки запроса);
    # proxy_headers=False — X-Forwarded-* разбирает только ProxyHeadersMiddleware для доверенных прокси
    uvicorn.run("dms_api.app:app", host=args.host, port=args.port, access_log=False, server_header=False,
                proxy_headers=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms", description="Ассистент менеджера сопровождения ДМС")
    parser.add_argument("--version", action="version", version=f"dms {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("kit", help="опись комплекта организаторов (data/raw)")
    p.add_argument("--json", action="store_true", help="вывести в JSON")
    p.set_defaults(func=_cmd_kit)
    p = sub.add_parser("config-check", help="проверить, что все конфиги читаются")
    p.set_defaults(func=_cmd_config_check)
    p = sub.add_parser("serve", help="запустить веб-сервис")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=_cmd_serve)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
