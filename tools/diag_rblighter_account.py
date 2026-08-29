#!/usr/bin/env python3
"""
诊断脚本：打印 rblighter 账户原始返回，用于排查 Web 余额显示为 0。

用法：
    cd ~/entropy-rblighter
    source .venv/bin/activate
    python3 diag_rblighter_account.py

需要 .env 里配齐：
    LIGHTER_ACCOUNT_INDEX
    LIGHTER_API_KEY_INDEX
    LIGHTER_API_PRIVATE_KEY
"""
import os
import sys
import asyncio
from pathlib import Path

ENV_PATH = Path.home() / "entropy-rblighter" / ".env"
BASE_URL = "https://api.rh.lighter.xyz"


def _load_dotenv(path: Path) -> dict:
    out = {}
    if not path.exists():
        print(f"找不到 .env: {path}")
        sys.exit(1)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


async def main():
    env = _load_dotenv(ENV_PATH)
    idx = env.get("LIGHTER_ACCOUNT_INDEX", "").strip()
    key_idx = env.get("LIGHTER_API_KEY_INDEX", "").strip()
    pk = env.get("LIGHTER_API_PRIVATE_KEY", "").strip()

    print(f"LIGHTER_ACCOUNT_INDEX={idx}")
    print(f"LIGHTER_API_KEY_INDEX={key_idx}")
    print(f"LIGHTER_API_PRIVATE_KEY 长度={len(pk)} 前缀={pk[:6] if pk else '空'}...")
    print(f"查询地址: {BASE_URL}")
    print("-" * 60)

    if not (idx and key_idx and pk):
        print("错误：.env 中 LIGHTER_ACCOUNT_INDEX / LIGHTER_API_KEY_INDEX / LIGHTER_API_PRIVATE_KEY 不全")
        sys.exit(1)

    try:
        import lighter
        from lighter import AccountApi, SignerClient
    except ImportError as e:
        print(f"导入 lighter 失败：{e}")
        print("请确认已执行：source .venv/bin/activate")
        sys.exit(1)

    try:
        signer = SignerClient(
            url=BASE_URL,
            api_private_keys={int(key_idx): pk},
            account_index=int(idx),
        )
        auth_token, err = signer.create_auth_token_with_expiry(
            deadline=3600, api_key_index=int(key_idx)
        )
        if err:
            print(f"生成鉴权 token 失败：{err}")
            sys.exit(1)
        print(f"鉴权 token 已生成（前 40 字符）：{auth_token[:40]}...")
    except Exception as e:
        print(f"SignerClient 初始化失败（签名二进制可能缺失）：{e}")
        sys.exit(1)

    config = lighter.Configuration(host=BASE_URL)
    config.api_key["apiKey"] = auth_token
    client = lighter.ApiClient(configuration=config)
    account_api = AccountApi(client)

    try:
        account = await account_api.account(by="index", value=str(int(idx)))
        acc = account.to_dict() if hasattr(account, "to_dict") else account
        print("-" * 60)
        print("原始返回字段（完整）：")
        for k, v in acc.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"查询 account 失败：{e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
