import os
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    # 设置 1920x1080 分辨率窗口
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()

    page.goto("https://baidu.com", wait_until="networkidle")
    print(page.title())

    # 百度首页已改版，搜索框为 textarea#chat-textarea
    page.fill("#chat-textarea", "Playwright")
    # 用回车键触发搜索，并等待导航完成
    with page.expect_navigation(wait_until="networkidle"):
        page.keyboard.press("Enter")
    # 等待搜索结果渲染
    page.wait_for_selector("#content_left", timeout=15000)
    path = os.path.join(os.getcwd(), "search.png")
    print(f"Screenshot saved to: {path}")
    page.screenshot(path=path)
    browser.close()
