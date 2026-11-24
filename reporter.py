# reporter.py
import asyncio
import json
import random
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import async_playwright

LogCB = Callable[[str, str], Awaitable[None]]

class ReportEngine:
    def __init__(self, config: dict, log_cb: LogCB, stop_event: asyncio.Event):
        self.cfg = config
        self.log = log_cb
        self.stop = stop_event
        self.stats = {
            "total": int(config["total_reports"]),
            "sent": 0,
            "success": 0,
            "failed": 0,
            "accounts_done": 0,
            "accounts_total": 0,
        }
        self.sessions_dir = Path("sessions")

    async def _emit_stats(self):
        await self.log("STATS", json.dumps(self.stats))

    async def _sleep_until_stop(self, seconds: int) -> bool:
        for _ in range(seconds):
            if self.stop.is_set():
                return False
            await asyncio.sleep(1)
        return True

    async def _find(self, page, selectors, timeout=12000):
        per = max(1500, timeout // max(1, len(selectors)))
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=per)
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _load_session(self, context, account: str) -> bool:
        f = self.sessions_dir / f"{account}.json"
        if not f.exists():
            return False
        try:
            state = json.loads(f.read_text())
            await context.add_cookies(state.get("cookies", []))
            return True
        except Exception:
            return False

    async def _save_session(self, context, account: str):
        try:
            self.sessions_dir.mkdir(exist_ok=True)
            state = await context.storage_state()
            (self.sessions_dir / f"{account}.json").write_text(json.dumps(state))
        except Exception:
            pass

    async def run(self):
        cfg = self.cfg
        proxies = [p.strip() for p in cfg["proxies"].splitlines() if p.strip()]
        accounts = [a.strip() for a in cfg["accounts"].splitlines() if a.strip()]

        if not proxies:
            await self.log("ERROR", "proxies kosong. isi dulu.")
            return
        if not accounts:
            await self.log("ERROR", "accounts kosong. isi dulu.")
            return

        target_jid = f"{cfg['target'].strip()}@s.whatsapp.net"
        per_account = int(cfg["reports_per_account"])
        
        # Hitung berapa akun yang dibutuhkan berdasarkan input total report
        needed = (int(cfg["total_reports"]) + per_account - 1) // per_account
        self.stats["accounts_total"] = min(needed, len(accounts))

        await self.log("INFO", f"Target        : {target_jid}")
        await self.log("INFO", f"Total report  : {cfg['total_reports']}")
        await self.log("INFO", f"Akun tersedia : {len(accounts)}")
        await self.log("INFO", f"Akun dipakai  : {self.stats['accounts_total']}")
        await self._emit_stats()

        for idx in range(self.stats["accounts_total"]):
            if self.stop.is_set():
                await self.log("WARN", "Stop diminta. Berhenti.")
                break

            # Sisa report yang harus dikirim
            remaining = int(cfg["total_reports"]) - self.stats["sent"]
            if remaining <= 0:
                break
                
            # Jika sisa report lebih sedikit dari jatah per akun, kurangi
            reports_this = min(per_account, remaining)

            proxy = random.choice(proxies)
            account = accounts[idx]

            await self._run_account(idx + 1, account, proxy, target_jid, reports_this)

            if self.stop.is_set():
                break

            delay = random.randint(int(cfg["account_delay_min"]), int(cfg["account_delay_max"]))
            await self.log("INFO", f"Delay akun berikutnya {delay}s...")
            if not await self._sleep_until_stop(delay):
                break

        await self.log("INFO", f"SELESAI. Sukses={self.stats['success']} Gagal={self.stats['failed']}")
        await self._emit_stats()

    async def _run_account(self, num: int, account: str, proxy: str, target_jid: str, reports_count: int):
        cfg = self.cfg
        await self.log("INFO", f"=== Akun #{num} ({account}) ===")

        launch_opts = {"headless": bool(cfg["headless"])}
        if proxy:
            launch_opts["proxy"] = {"server": proxy}

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(**launch_opts)
            except Exception as e:
                await self.log("ERROR", f"Akun #{num} launch gagal: {e}")
                return

            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                )
                page = await context.new_page()

                await self.log("INFO", f"Akun #{num}: buka WA Web...")
                try:
                    await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    await self.log("ERROR", f"Akun #{num} gagal buka: {e}")
                    return

                loaded = await self._load_session(context, account)
                if loaded:
                    await self.log("INFO", f"Akun #{num}: coba session tersimpan...")
                    try:
                        await page.reload(wait_until="domcontentloaded")
                    except Exception:
                        pass
                    try:
                        await page.wait_for_selector('div[aria-label="Chat list"]', timeout=15000)
                        await self.log("OK", f"Akun #{num}: login via session.")
                    except Exception:
                        loaded = False

                if not loaded:
                    await self.log("INFO", f"Akun #{num}: scan QR dalam 120 detik...")
                    try:
                        await page.wait_for_selector('div[aria-label="Chat list"]', timeout=120000)
                        await self.log("OK", f"Akun #{num}: login via QR.")
                        await self._save_session(context, account)
                    except Exception:
                        await self.log("ERROR", f"Akun #{num}: login timeout. Skip.")
                        await browser.close()
                        return

                ok_cnt = 0
                fail_cnt = 0
                for i in range(reports_count):
                    if self.stop.is_set():
                        break
                    ok = await self._report(page, target_jid, num)
                    self.stats["sent"] += 1
                    if ok:
                        ok_cnt += 1
                        self.stats["success"] += 1
                        await self.log("OK", f"Akun #{num} report {i+1}/{reports_count} OK")
                    else:
                        fail_cnt += 1
                        self.stats["failed"] += 1
                        await self.log("FAIL", f"Akun #{num} report {i+1}/{reports_count} GAGAL")

                    await self._emit_stats()

                    delay = random.randint(int(cfg["report_delay_min"]), int(cfg["report_delay_max"]))
                    await self.log("INFO", f"Delay {delay}s...")
                    if not await self._sleep_until_stop(delay):
                        break

                self.stats["accounts_done"] += 1
                await self._emit_stats()
                await self.log("INFO", f"Akun #{num} selesai: OK={ok_cnt} GAGAL={fail_cnt}")
                await browser.close()
            except Exception as e:
                await self.log("ERROR", f"Akun #{num} crash: {e}")
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _report(self, page, target_jid: str, num: int) -> bool:
        try:
            search = await self._find(page, ['div[contenteditable="true"]', 'div[aria-label="Search input textbox"]'])
            if not search: return False
            await search.click()
            await search.fill(target_jid)
            await page.wait_for_timeout(2000)

            phone = target_jid.split("@")[0]
            chat = await self._find(page, ['div[role="listitem"]', f'div[aria-label*="{phone}"]'])
            if not chat: return False
            await chat.click()
            await page.wait_for_timeout(2000)

            menu = await self._find(page, ['div[aria-label="Menu"]', 'span[data-icon="menu"]', 'div[title="Menu"]'])
            if not menu: return False
            await menu.click()
            await page.wait_for_timeout(1000)

            rep = await self._find(page, ['div[aria-label="Report"]', 'li[aria-label="Report"]', 'div[role="button"]:has-text("Report")'])
            if not rep: return False
            await rep.click()
            await page.wait_for_timeout(1000)

            opt = await self._find(page, ['div[role="radio"]', 'input[type="radio"]'], timeout=6000)
            if opt:
                await opt.click()
                await page.wait_for_timeout(500)

            btn = await self._find(page, ['div[role="button"]:has-text("Report")', 'button:has-text("Report")', 'div[role="button"]:has-text("Kirim")'], timeout=8000)
            if not btn: return False
            await btn.click()
            await page.wait_for_timeout(1500)
            return True
        except Exception as e:
            await self.log("ERROR", f"Akun #{num} report error: {e}")
            return False
