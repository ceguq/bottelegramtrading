import sys
from pathlib import Path

import MetaTrader5 as mt5

from mt5_executor import MT5_PATH


def main() -> int:
    print(f"Configured MT5 path: {MT5_PATH}")

    mt5_executable = Path(MT5_PATH)
    if not mt5_executable.is_file():
        print(f"ERROR: MT5 executable not found at configured path: {MT5_PATH}")
        print(
            "To find the correct path:\n"
            "1) Right-click the Valetax MT5 desktop shortcut\n"
            "2) Select Properties\n"
            "3) Copy the Target path pointing to terminal64.exe\n"
            "4) Update MT5_PATH in mt5_executor.py"
        )
        return 1

    initialized = False
    try:
        initialized = mt5.initialize(path=MT5_PATH)
        if not initialized:
            print("ERROR: MT5 initialize(path=MT5_PATH) failed.")
            print(mt5.last_error())
            return 1

        terminal = mt5.terminal_info()
        account = mt5.account_info()

        if terminal is None:
            print("ERROR: MT5 terminal_info() is unavailable.")
            print(mt5.last_error())
            return 1

        if account is None:
            print("ERROR: No logged-in MT5 account detected (account_info() is unavailable).")
            print(mt5.last_error())
            return 1

        company = getattr(terminal, "company", "Unavailable")
        connected = getattr(terminal, "connected", "Unavailable")
        trade_allowed = getattr(terminal, "trade_allowed", "Unavailable")
        terminal_path = getattr(terminal, "path", "Unavailable")

        account_login = getattr(account, "login", "Unavailable")
        balance = getattr(account, "balance", "Unavailable")
        server = getattr(account, "server", "Unavailable")

        print(f"Company: {company}")
        print(f"Connected: {connected}")
        print(f"Trade allowed: {trade_allowed}")
        print(f"Terminal path: {terminal_path}")
        print(f"Account login: {account_login}")
        print(f"Balance: {balance}")
        print(f"Server: {server}")

        company_str = str(company).lower()
        if "metaqoutes" in company_str:
            print(
                "WARNING: Python is connected to the MetaQuotes terminal, not the expected Valetax terminal."
            )
            return 1

        if connected is not True:
            print("MT5 terminal is not connected.")
            return 1

        if trade_allowed is not True:
            print(
                "MT5 trade not allowed. Check: 1) Algo Trading enabled in MT5, 2) Correct terminal path, 3) Account logged in"
            )
            return 1

        print("MT5 connection OK — ready to trade")
        return 0
    finally:
        try:
            mt5.shutdown()
        except Exception:
            # Always attempt shutdown; ignore shutdown errors.
            pass


if __name__ == "__main__":
    raise SystemExit(main())

