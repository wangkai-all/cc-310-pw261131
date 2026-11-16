#!/usr/bin/env python3
"""启动分析页服务:python3 run.py [--port 8000] [--reseed]"""
import argparse
import os

from churnwatch.db import DB_PATH, connect, init_db, is_seeded
from churnwatch.server import make_server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reseed", action="store_true", help="删除现有数据库并重新生成虚构样本")
    args = ap.parse_args()

    if args.reseed and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect()
    init_db(conn)
    if not is_seeded(conn):
        from churnwatch.seed import seed
        print("生成虚构样本数据 ...")
        seed(conn)
    conn.close()

    server = make_server(args.host, args.port)
    print(f"分析页: http://{args.host}:{args.port}/  (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
