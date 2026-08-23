# -*- coding: utf-8 -*-
"""Meta.ai 登录脚本：启动浏览器 → 先连接 Urban VPN → 打开 meta.ai 让用户登录。

登录状态通过持久化 profile（meta_profile）自动保存。
"""
import asyncio
import logging

logger = logging.getLogger("meta-login")

META_LOGIN_WAIT_SEC = 600


async def login_and_save(show_browser: bool = True) -> dict:
    """
    启动 meta 浏览器（必须支持扩展插件，内置 Urban VPN），
    先确保 VPN 连接成功，再打开 https://www.meta.ai/ 让用户登录。

    Args:
        show_browser: 是否显示浏览器窗口（headless=False）

    Returns:
        {"success": True/False, "message": ...}
    """
    from browser_client import browser_client

    try:
        # ensure_meta_ready 保证顺序：启动浏览器(启用扩展) → VPN 连接成功 → 打开 meta.ai
        await browser_client.ensure_meta_ready(headless=not show_browser, ensure_vpn=True)

        page = browser_client._meta_page
        if not page or page.is_closed():
            return {"success": False, "message": "Meta page not available"}

        async def _login_state():
            try:
                return await page.evaluate("""() => {
                    const t = (document.body.innerText || '').trim();
                    const hasComposer = !!document.querySelector('textarea');
                    const hasLoginBtn = [...document.querySelectorAll('a,button,[role=button]')].some(
                        x => /^log in$|^登录$/i.test((x.textContent || '').trim())
                    );
                    return { hasComposer, hasLoginBtn, len: t.length };
                }""")
            except Exception:
                return None

        st = await _login_state()
        if st and st.get("hasComposer"):
            logger.info("Already logged in (composer detected)")
        else:
            if show_browser:
                print("=" * 50)
                print("请在浏览器中登录 Meta.ai 账号 (https://www.meta.ai/)")
                print("登录成功后程序将自动检测并继续")
                print("=" * 50)
            logger.info("Waiting for manual login...")
            ok = False
            for _ in range(META_LOGIN_WAIT_SEC * 2):
                await asyncio.sleep(0.5)
                if not browser_client._meta_page or browser_client._meta_page.is_closed():
                    return {"success": False, "message": "浏览器被关闭，登录取消"}
                st = await _login_state()
                if st and (st.get("hasComposer") or (not st.get("hasLoginBtn") and st.get("len", 0) > 50)):
                    ok = True
                    break
            if not ok:
                await browser_client.close_meta()
                return {"success": False, "message": f"登录超时（{META_LOGIN_WAIT_SEC // 60} 分钟），请重试"}
            logger.info("Login detected")

        logger.info("Login successful. Session saved to meta_profile directory.")
        print("=" * 50)
        print("登录成功！会话状态已保存到 meta_profile 目录。")
        print("=" * 50)
        print("请手动关闭浏览器窗口以退出程序...")

        # 等待用户关闭浏览器窗口后结束
        for _ in range(1200):
            await asyncio.sleep(0.5)
            if not browser_client._meta_page or browser_client._meta_page.is_closed():
                break

        await browser_client.close_meta()
        return {"success": True, "message": "Meta.ai login completed"}
    except Exception as e:
        logger.error(f"Meta login error: {e}")
        return {"success": False, "message": str(e)}


if __name__ == '__main__':
    asyncio.run(login_and_save(show_browser=True))
