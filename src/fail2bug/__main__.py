import argparse


def main():
    parser = argparse.ArgumentParser(description="Fail2Bug · 失败用例批量提单")
    parser.add_argument("command", choices=["serve", "bot", "verify", "import"], nargs="?", default="serve")
    parser.add_argument("--host", help="监听地址，默认使用 F2B_HOST 或 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--source", help="verify 时检查的执行表 sheets 链接")
    parser.add_argument('--file',help='本地 .xlsx 或 .zip 路径')
    parser.add_argument('--sheet',help='本地 Excel 工作表名称，默认第一个')
    args = parser.parse_args()
    if args.command == "bot":
        from .bot import run_bot
        run_bot()
    elif args.command == 'import':
        from pathlib import Path
        from .config import Settings
        from .feishu import FeishuClient
        from .service import Service
        from .store import Store
        if not args.file:parser.error('import 需要 --file')
        settings=Settings();client=FeishuClient(settings)
        try:
            batch=Service(settings,Store(settings.database),client).import_file(Path(args.file),sheet=args.sheet)
            print(f"已生成草稿：{len(batch['items'])} 条失败 / {sum(len(item['attachments']) for item in batch['items'])} 个附件")
            print('批次：'+batch['id'])
            print('网页审核：'+settings.base_url.rstrip('/')+'/batches/'+batch['id'])
            print('聊天继续：使用 '+batch['id'][:8])
            for warning in batch.get('warnings',[]):print('提示：'+warning)
        finally:client.close()
    elif args.command == "verify":
        from .config import Settings
        from .feishu import FeishuClient, FeishuError
        from .parser import InputError, parse_table
        settings = Settings()
        if settings.mode != "live":
            raise SystemExit("请先在 .env 中配置 live 模式及飞书应用信息。")
        client = FeishuClient(settings)
        failed = False
        try:
            try:
                client._access_token()
                print("[通过] 应用鉴权")
            except FeishuError as exc:
                raise SystemExit(f"[失败] 应用鉴权：{exc}") from None
            try:
                client.verify_fields()
                print("[通过] 缺陷表访问及字段映射")
            except FeishuError as exc:
                failed = True
                print(f"[失败] 缺陷表：{exc}")
            if args.source:
                try:
                    rows, _, canonical = client.read_sheet(args.source)
                    parsed = parse_table(rows, canonical)
                    print(f"[通过] 执行表读取：{sum(parsed['counts'].values())} 条用例，{len(parsed['items'])} 条失败")
                except (FeishuError, InputError) as exc:
                    failed = True
                    print(f"[失败] 执行表：{exc}")
        finally:
            client.close()
        if failed:
            raise SystemExit(1)
    else:
        import uvicorn
        from .app import create_app
        from .config import Settings
        settings=Settings()
        uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port)


if __name__ == "__main__":
    main()
