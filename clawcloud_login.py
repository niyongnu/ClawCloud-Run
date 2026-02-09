#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClawCloud 自动登录脚本 - 青龙版
cron: 0 8 */3 * *
new Env('ClawCloud自动登录');
"""

import os
import sys
import time
import re
import random
import requests
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException, TimeoutException

# ==================== 配置 ====================
# 代理配置 (留空则不使用)
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 登录入口，根据自己实际区域修改
LOGIN_ENTRY_URL = "https://eu-central-1.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"

DEVICE_VERIFY_WAIT = 30
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))
QL_URL = os.environ.get("QL_URL", "http://127.0.0.1:5700")
CHROME_DRIVER_PATH = '/usr/bin/chromedriver'
CHROME_BINARY_PATH = '/usr/bin/chromium-browser'


class QingLong:
    """青龙面板 API"""
    
    def __init__(self):
        self.client_id = os.environ.get('QL_CLIENT_ID')
        self.client_secret = os.environ.get('QL_CLIENT_SECRET')
        self.base_url = QL_URL
        self.token = None
        self.ok = bool(self.client_id and self.client_secret)
        if self.ok:
            self._get_token()

    def _get_token(self):
        try:
            r = requests.get(f"{self.base_url}/open/auth/token",
                           params={"client_id": self.client_id, "client_secret": self.client_secret}, timeout=30)
            data = r.json()
            if data.get("code") == 200:
                self.token = data["data"]["token"]
                print("✅ 青龙 API Token 获取成功")
                return True
            self.ok = False
        except Exception as e:
            print(f"❌ 获取青龙 Token 异常: {e}")
            self.ok = False
        return False

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def get_env(self, name):
        if not self.ok:
            return None
        try:
            r = requests.get(f"{self.base_url}/open/envs", headers=self._headers(),
                           params={"searchValue": name}, timeout=30)
            data = r.json()
            if data.get("code") == 200:
                for env in data.get("data", []):
                    if env.get("name") == name:
                        return env
        except Exception:
            pass
        return None

    def update_env(self, name, value, remarks=""):
        if not self.ok:
            return False
        try:
            existing = self.get_env(name)
            if existing:
                payload = {"id": existing["id"], "name": name, "value": value, "remarks": remarks or existing.get("remarks", "")}
                r = requests.put(f"{self.base_url}/open/envs", headers=self._headers(), json=payload, timeout=30)
            else:
                r = requests.post(f"{self.base_url}/open/envs", headers=self._headers(),
                                json=[{"name": name, "value": value, "remarks": remarks}], timeout=30)
            if r.json().get("code") == 200:
                print(f"✅ 环境变量 {name} 更新成功")
                return True
        except Exception as e:
            print(f"❌ 更新环境变量异常: {e}")
        return False


class Telegram:
    """Telegram 通知"""
    
    def __init__(self):
        self.token = os.environ.get('TG_BOT_TOKEN')
        self.chat_id = os.environ.get('TG_CHAT_ID')
        self.ok = bool(self.token and self.chat_id)

    def send(self, msg):
        if not self.ok:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                        data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"}, timeout=30)
        except Exception:
            pass

    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path):
            return
        try:
            with open(path, 'rb') as f:
                requests.post(f"https://api.telegram.org/bot{self.token}/sendPhoto",
                            data={"chat_id": self.chat_id, "caption": caption[:1024]}, files={"photo": f}, timeout=60)
        except Exception:
            pass

    def flush_updates(self):
        if not self.ok:
            return 0
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates", params={"timeout": 0}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"):
                return data["result"][-1]["update_id"] + 1
        except Exception:
            pass
        return 0

    def wait_code(self, timeout=120):
        if not self.ok:
            return None
        offset = self.flush_updates()
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")
        while time.time() < deadline:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                               params={"timeout": 20, "offset": offset}, timeout=30)
                data = r.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    chat = msg.get("chat") or {}
                    if str(chat.get("id")) != str(self.chat_id):
                        continue
                    text = (msg.get("text") or "").strip()
                    match = pattern.match(text)
                    if match:
                        return match.group(1)
            except Exception:
                pass
            time.sleep(2)
        return None


class ClawCloudAutoLogin:
    def __init__(self):
        self.username = os.environ.get('GH_USERNAME')
        self.password = os.environ.get('GH_PASSWORD')
        self.gh_session = os.environ.get('GH_SESSION', '').strip()
        self.telegram = Telegram()
        self.qinglong = QingLong()
        self.driver = None
        self.screenshots = []
        self.logs = []
        self.screenshot_counter = 0
        self.new_cookie = None
        self.final_screenshot_path = None
        self.login_verified = False
        
        # 区域相关
        self.detected_region = 'eu-central-1'
        self.region_base_url = LOGIN_ENTRY_URL

    def init_driver(self):
        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-infobars')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        # 代理配置
        if PROXY_DSN:
            try:
                p_url = urlparse(PROXY_DSN)
                proxy_server = f"{p_url.hostname}:{p_url.port}"
                options.add_argument(f'--proxy-server={p_url.scheme}://{proxy_server}')
                self.log(f"启用代理: {p_url.scheme}://{proxy_server}")
            except Exception as e:
                self.log(f"代理配置解析失败: {e}", "ERROR")
        
        options.binary_location = CHROME_BINARY_PATH
        service = Service(CHROME_DRIVER_PATH)
        self.driver = webdriver.Chrome(service=service, options=options)
        
        # 反检测脚本
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                window.chrome = { runtime: {} };
            '''
        })
        
        self.driver.implicitly_wait(10)
        self.log("Chrome 浏览器驱动初始化成功", "SUCCESS")

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line)
        self.logs.append(line)

    def capture_screenshot(self, name):
        self.screenshot_counter += 1
        filename = f"/tmp/{self.screenshot_counter:02d}_{name}.png"
        try:
            self.driver.save_screenshot(filename)
            self.screenshots.append(filename)
            return filename
        except Exception:
            return None

    def find_and_click(self, selectors, description=""):
        for sel_type, sel in selectors:
            try:
                elem = self.driver.find_element(By.XPATH if sel_type == "xpath" else By.CSS_SELECTOR, sel)
                if elem.is_displayed() and elem.is_enabled():
                    time.sleep(random.uniform(0.5, 1.5))
                    elem.click()
                    if description:
                        self.log(f"已点击: {description}", "SUCCESS")
                    return True
            except Exception:
                continue
        return False

    def detect_region(self, url):
        """从 URL 中检测区域信息"""
        try:
            parsed = urlparse(url)
            host = parsed.netloc
            
            if host.endswith('.console.claw.cloud'):
                region = host.replace('.console.claw.cloud', '')
                if region and region != 'console':
                    self.detected_region = region
                    self.region_base_url = f"https://{host}"
                    self.log(f"检测到区域: {region}", "SUCCESS")
                    return region
            
            if '.run.claw.cloud' in host:
                parts = host.split('.run.claw.cloud')[0]
                if parts and parts != 'console':
                    self.detected_region = parts
                    self.region_base_url = f"https://{host}"
                    self.log(f"检测到区域: {parts}", "SUCCESS")
                    return parts
            
            self.log(f"未检测到特定区域，使用当前域名: {host}", "INFO")
            self.region_base_url = f"{parsed.scheme}://{parsed.netloc}"
            return None
            
        except Exception as e:
            self.log(f"区域检测异常: {e}", "WARN")
            return None

    def get_base_url(self):
        return self.region_base_url if self.region_base_url else LOGIN_ENTRY_URL

    def get_github_cookie(self):
        try:
            for cookie in self.driver.get_cookies():
                if cookie['name'] == 'user_session' and 'github' in cookie.get('domain', ''):
                    return cookie['value']
        except Exception:
            pass
        return None

    def inject_github_cookies(self):
        if not self.gh_session:
            return False
        try:
            self.driver.get("https://github.com")
            time.sleep(2)
            self.driver.add_cookie({'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'})
            self.driver.add_cookie({'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'})
            self.driver.refresh()
            time.sleep(3)
            if 'login' in self.driver.current_url:
                self.log("Cookie 已失效", "WARN")
                return False
            self.log("GitHub Cookie 注入成功", "SUCCESS")
            return True
        except Exception as e:
            self.log(f"Cookie 注入失败: {e}", "WARN")
            return False

    def save_cookie_to_env(self, cookie_value):
        if not cookie_value or cookie_value == self.gh_session:
            self.log("Cookie 未变化", "INFO")
            return False
        self.log(f"新 Cookie: {cookie_value[:15]}...{cookie_value[-8:]}", "SUCCESS")
        if self.qinglong.update_env('GH_SESSION', cookie_value, 'GitHub Session Cookie - 自动更新'):
            self.telegram.send("🔑 <b>Cookie 已自动更新</b>")
        else:
            self.telegram.send(f"🔑 请手动更新 GH_SESSION:\n<code>{cookie_value}</code>")
        return True

    def get_page_type(self):
        try:
            url = self.driver.current_url.lower()
            if 'github.com' in url:
                if 'two-factor' in url:
                    return 'github_2fa'
                if '/login' in url or '/session' in url:
                    return 'github_login'
                if '/login/oauth/authorize' in url:
                    return 'github_oauth'
                return 'github_other'
            if 'claw.cloud' in url:
                if '/callback' in url:
                    return 'callback'
                if '/signin' in url:
                    return 'signin'
                return 'console'
            return 'unknown'
        except Exception:
            return 'unknown'

    def is_in_console(self):
        try:
            url = self.driver.current_url.lower()
            if '/signin' in url or '/callback' in url or 'github.com' in url:
                return False
            if 'claw.cloud' not in url:
                return False
            page = self.driver.page_source.lower()
            for sign in ['sign in with github', 'continue with github']:
                if sign in page:
                    return False
            return True
        except Exception:
            return False

    def wait_for_callback_complete(self, timeout=30):
        self.log("等待 OAuth callback 处理...", "STEP")
        for i in range(timeout):
            page_type = self.get_page_type()
            if i % 3 == 0:
                self.log(f"[{i}s] 类型: {page_type}")
            if page_type == 'console':
                self.detect_region(self.driver.current_url)
                self.log(f"Callback 完成，域名: {self.region_base_url}", "SUCCESS")
                return True
            if page_type == 'signin':
                self.log("Callback 后返回登录页", "ERROR")
                return False
            if page_type in ['github_login', 'github_oauth', 'github_2fa']:
                return 'need_github'
            time.sleep(1)
        self.log("Callback 超时", "ERROR")
        return False

    def handle_device_verification(self):
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.telegram.send(f"⚠️ <b>需要设备验证</b>\n请在 {DEVICE_VERIFY_WAIT} 秒内批准：\n1️⃣ 检查邮箱点击链接\n2️⃣ 或在 GitHub App 批准")
        shot = self.capture_screenshot("设备验证")
        if shot:
            self.telegram.photo(shot, "设备验证页面")
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            url = self.driver.current_url
            if 'verified-device' not in url and 'device-verification' not in url:
                self.log("设备验证通过！", "SUCCESS")
                self.telegram.send("✅ <b>设备验证通过</b>")
                return True
            if i % 5 == 0:
                self.log(f"等待... ({i}/{DEVICE_VERIFY_WAIT}秒)")
                try:
                    self.driver.refresh()
                    time.sleep(2)
                except Exception:
                    pass
        return 'verified-device' not in self.driver.current_url

    def handle_two_factor_mobile(self):
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")
        shot = self.capture_screenshot("两步验证_mobile")
        self.telegram.send(f"⚠️ <b>需要两步验证（GitHub Mobile）</b>\n\n请打开手机 GitHub App 批准本次登录（会让你确认一个数字）。\n等待时间：{TWO_FACTOR_WAIT} 秒")
        if shot:
            self.telegram.photo(shot, "两步验证页面（数字在图里）")
        
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            url = self.driver.current_url
            if "github.com/sessions/two-factor/" not in url:
                self.log("两步验证通过！", "SUCCESS")
                self.telegram.send("✅ <b>两步验证通过</b>")
                return True
            if "github.com/login" in url and 'two-factor' not in url:
                self.log("两步验证后回到了登录页，需重新登录", "ERROR")
                return False
            if i % 10 == 0 and i != 0:
                self.log(f"等待中... ({i}/{TWO_FACTOR_WAIT}秒)")
                shot = self.capture_screenshot(f"两步验证_{i}s")
                if shot:
                    self.telegram.photo(shot, f"两步验证页面（第{i}秒）")
            if i % 30 == 0 and i != 0:
                try:
                    self.driver.refresh()
                    time.sleep(2)
                except:
                    pass
        
        self.log("两步验证超时", "ERROR")
        self.telegram.send("❌ <b>两步验证超时</b>")
        return False

    def handle_two_factor_code(self):
        self.log("需要输入验证码", "WARN")
        shot = self.capture_screenshot("两步验证_code")
        
        # 如果是 Security Key (webauthn) 页面，尝试切换到 Authenticator App
        if 'two-factor/webauthn' in self.driver.current_url:
            self.log("检测到 Security Key 页面，尝试切换...", "INFO")
            try:
                more_options_selectors = [
                    ("xpath", "//button[contains(text(),'More options')]"),
                    ("css", "button.js-webauthn-other-options")
                ]
                if self.find_and_click(more_options_selectors, "More options"):
                    time.sleep(1)
                    self.capture_screenshot("点击more_options后")
                    
                    auth_app_selectors = [
                        ("xpath", "//button[contains(text(),'Authenticator app')]"),
                        ("css", "button[data-type='app']")
                    ]
                    if self.find_and_click(auth_app_selectors, "Authenticator app"):
                        time.sleep(2)
                        shot = self.capture_screenshot("切换到验证码输入页")
            except Exception as e:
                self.log(f"切换验证方式时出错: {e}", "WARN")
        
        # 尝试切换到验证码模式
        switch_selectors = [
            ("xpath", "//a[contains(text(),'Use your authenticator app')]"),
            ("xpath", "//a[contains(text(),'authentication app')]"),
            ("xpath", "//a[contains(text(),'Enter a code')]"),
            ("xpath", "//button[contains(text(),'Authenticator app')]"),
            ("css", "[href*='two-factor/app']")
        ]
        for sel_type, sel in switch_selectors:
            try:
                elem = self.driver.find_element(By.XPATH if sel_type == "xpath" else By.CSS_SELECTOR, sel)
                if elem.is_displayed():
                    elem.click()
                    self.log("已切换到验证码模式", "SUCCESS")
                    time.sleep(2)
                    shot = self.capture_screenshot("两步验证_code_切换后")
                    break
            except Exception:
                continue

        self.telegram.send(f"🔐 <b>需要验证码登录</b>\n\n用户 {self.username} 正在登录，请在 Telegram 里发送：\n<code>/code 你的6位验证码</code>\n\n等待时间：{TWO_FACTOR_WAIT} 秒")
        if shot:
            self.telegram.photo(shot, "两步验证页面")

        code = self.telegram.wait_code(timeout=TWO_FACTOR_WAIT)
        if not code:
            self.log("等待验证码超时", "ERROR")
            self.telegram.send("❌ <b>等待验证码超时</b>")
            return False

        self.log("收到验证码，正在填入...", "SUCCESS")
        self.telegram.send("✅ 收到验证码，正在填入...")

        input_selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name="otp"]',
            'input#app_totp',
            'input#otp',
            'input[inputmode="numeric"]'
        ]
        
        for sel in input_selectors:
            try:
                elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                if elem.is_displayed() and elem.is_enabled():
                    elem.click()
                    time.sleep(random.uniform(0.2, 0.5))
                    elem.clear()
                    for c in code:
                        elem.send_keys(c)
                        time.sleep(random.uniform(0.05, 0.15))
                    self.log("验证码已输入", "SUCCESS")
                    time.sleep(1)
                    
                    submitted = False
                    verify_selectors = [
                        ("xpath", "//button[contains(text(),'Verify')]"),
                        ("css", "button[type='submit']"),
                        ("css", "input[type='submit']")
                    ]
                    for btn_type, btn_sel in verify_selectors:
                        try:
                            btn = self.driver.find_element(By.XPATH if btn_type == "xpath" else By.CSS_SELECTOR, btn_sel)
                            if btn.is_displayed() and btn.is_enabled():
                                btn.click()
                                submitted = True
                                self.log("已点击 Verify 按钮", "SUCCESS")
                                break
                        except:
                            pass
                    
                    if not submitted:
                        time.sleep(random.uniform(0.3, 0.8))
                        elem.send_keys(Keys.RETURN)
                        self.log("已按 Enter 提交", "SUCCESS")
                    
                    time.sleep(3)
                    self.capture_screenshot("验证码提交后")
                    
                    if "two-factor" not in self.driver.current_url:
                        self.log("验证码验证通过！", "SUCCESS")
                        self.telegram.send("✅ <b>验证码验证通过</b>")
                        cookie = self.get_github_cookie()
                        if cookie:
                            self.new_cookie = cookie
                        return True
                    else:
                        self.log("验证码可能错误", "ERROR")
                        self.telegram.send("❌ <b>验证码可能错误，请检查后重试</b>")
                        return False
            except Exception:
                continue

        self.log("没找到验证码输入框", "ERROR")
        self.telegram.send("❌ <b>没找到验证码输入框</b>")
        return False

    def login_to_github(self):
        self.log("登录 GitHub...", "STEP")
        self.capture_screenshot("github_登录页")
        try:
            user_input = self.driver.find_element(By.CSS_SELECTOR, 'input[name="login"]')
            user_input.click()
            time.sleep(random.uniform(0.3, 0.8))
            for c in self.username:
                user_input.send_keys(c)
                time.sleep(random.uniform(0.03, 0.1))
            
            time.sleep(random.uniform(0.5, 1.0))
            
            pass_input = self.driver.find_element(By.CSS_SELECTOR, 'input[name="password"]')
            pass_input.click()
            time.sleep(random.uniform(0.3, 0.8))
            for c in self.password:
                pass_input.send_keys(c)
                time.sleep(random.uniform(0.03, 0.1))
            
            self.log("已输入凭据", "SUCCESS")
        except Exception as e:
            self.log(f"输入凭据失败: {e}", "ERROR")
            return False

        self.capture_screenshot("github_已填写")

        try:
            self.driver.find_element(By.CSS_SELECTOR, 'input[type="submit"], button[type="submit"]').click()
        except Exception:
            pass

        time.sleep(3)
        self.capture_screenshot("github_登录后")
        url = self.driver.current_url
        self.log(f"当前: {url}")

        if 'verified-device' in url or 'device-verification' in url:
            if not self.handle_device_verification():
                return False
            time.sleep(2)
            url = self.driver.current_url

        if 'two-factor' in url:
            self.log("需要两步验证！", "WARN")
            self.capture_screenshot("两步验证")
            
            if 'two-factor/mobile' in url:
                if not self.handle_two_factor_mobile():
                    return False
            else:
                if not self.handle_two_factor_code():
                    return False
            time.sleep(2)

        try:
            err = self.driver.find_element(By.CSS_SELECTOR, '.flash-error')
            if err.is_displayed():
                self.log(f"错误: {err.text}", "ERROR")
                return False
        except:
            pass

        cookie = self.get_github_cookie()
        if cookie:
            self.new_cookie = cookie
            self.log("GitHub 登录成功", "SUCCESS")
        return True

    def handle_oauth_authorization(self):
        if 'github.com/login/oauth/authorize' not in self.driver.current_url:
            return False
        self.log("处理 OAuth 授权...", "STEP")
        self.capture_screenshot("oauth")
        cookie = self.get_github_cookie()
        if cookie:
            self.new_cookie = cookie
        selectors = [
            ("xpath", "//button[@name='authorize']"),
            ("xpath", "//button[contains(text(),'Authorize')]"),
            ("css", "button[name='authorize']")
        ]
        self.find_and_click(selectors, "OAuth 授权")
        time.sleep(3)
        return True

    def handle_github_flow(self):
        for _ in range(5):
            page_type = self.get_page_type()
            self.log(f"GitHub 流程: {page_type}")
            if page_type == 'github_login':
                if not self.login_to_github():
                    return False
                time.sleep(2)
            elif page_type == 'github_oauth':
                self.handle_oauth_authorization()
                time.sleep(2)
            elif page_type == 'github_2fa':
                if 'two-factor/mobile' in self.driver.current_url:
                    if not self.handle_two_factor_mobile():
                        return False
                else:
                    if not self.handle_two_factor_code():
                        return False
                time.sleep(2)
            elif page_type in ['console', 'callback', 'signin']:
                return True
            else:
                time.sleep(2)
        return True

    def wait_redirect(self, timeout=60):
        self.log("等待重定向...", "STEP")
        for i in range(timeout):
            url = self.driver.current_url
            
            if 'claw.cloud' in url and 'signin' not in url.lower():
                self.log("重定向成功！", "SUCCESS")
                self.detect_region(url)
                return True
            
            if 'github.com/login/oauth/authorize' in url:
                self.handle_oauth_authorization()
            
            time.sleep(1)
            if i % 10 == 0:
                self.log(f"等待... ({i}秒)")
        
        self.log("重定向超时", "ERROR")
        return False

    def perform_keepalive(self):
        self.log("执行保活...", "STEP")
        base_url = self.get_base_url()
        self.log(f"使用区域 URL: {base_url}", "INFO")
        
        pages_to_visit = [
            (f"{base_url}/", "控制台"),
            (f"{base_url}/apps", "应用"),
        ]
        
        if self.detected_region:
            self.log(f"当前区域: {self.detected_region}", "INFO")
        
        for url, name in pages_to_visit:
            try:
                self.driver.get(url)
                time.sleep(5)
                
                if '/signin' in self.driver.current_url.lower():
                    self.log(f"访问 {name} 被重定向到登录页！", "ERROR")
                    return False
                
                self.log(f"已访问: {name}", "SUCCESS")
                
                current_url = self.driver.current_url
                if 'claw.cloud' in current_url:
                    self.detect_region(current_url)
                
                time.sleep(2)
            except Exception as e:
                self.log(f"访问 {name} 失败: {e}", "WARN")
        
        self.final_screenshot_path = self.capture_screenshot("完成")
        self.log("保活成功！", "SUCCESS")
        return True

    def send_notification(self, success, error_message=""):
        if not self.telegram.ok:
            return
        
        region_info = f"\n<b>区域:</b> {self.detected_region or '默认'}"
        
        status = "✅ 成功" if success else "❌ 失败"
        msg = (f"<b>🤖 ClawCloud 自动登录</b>\n\n"
               f"<b>状态:</b> {status}\n"
               f"<b>用户:</b> {self.username}{region_info}\n"
               f"<b>时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}")
        if error_message:
            msg += f"\n<b>错误:</b> {error_message}"
        msg += f"\n\n<b>日志:</b>\n" + "\n".join(self.logs[-6:])
        self.telegram.send(msg)
        
        if success:
            if self.final_screenshot_path:
                self.telegram.photo(self.final_screenshot_path, "完成")
        else:
            for s in self.screenshots[-3:]:
                self.telegram.photo(s, s)

    def cleanup_resources(self):
        for s in self.screenshots:
            try:
                os.remove(s)
            except Exception:
                pass
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

    def execute_login_flow(self):
        print("\n" + "=" * 60)
        print("🚀 ClawCloud 自动登录 - 青龙版")
        print("=" * 60 + "\n")

        self.log(f"GitHub 用户名: {self.username}")
        self.log(f"现有 Session: {'有' if self.gh_session else '无'}")
        self.log(f"密码: {'有' if self.password else '无'}")
        self.log(f"登录入口: {LOGIN_ENTRY_URL}")
        self.log(f"青龙面板 API: {'已配置' if self.qinglong.ok else '未配置'}")
        self.log(f"Telegram 通知: {'已配置' if self.telegram.ok else '未配置'}")

        if not self.username or not self.password:
            self.log("缺少 GitHub 凭据", "ERROR")
            self.send_notification(False, "凭据未配置")
            sys.exit(1)

        try:
            self.init_driver()
            if self.gh_session:
                self.inject_github_cookies()

            self.log("步骤 1: 打开 ClawCloud 登录页", "STEP")
            self.driver.get(SIGNIN_URL)
            time.sleep(3)
            self.capture_screenshot("clawcloud")
            
            current_url = self.driver.current_url
            self.log(f"当前 URL: {current_url}")

            if 'signin' not in current_url.lower() and 'claw.cloud' in current_url and 'github.com' not in current_url:
                self.log("已登录！", "SUCCESS")
                self.detect_region(current_url)
                self.perform_keepalive()
                new_cookie = self.get_github_cookie()
                if new_cookie:
                    self.save_cookie_to_env(new_cookie)
                self.send_notification(True)
                print("\n✅ 成功！\n")
                return

            self.log("步骤 2: 点击 GitHub 登录", "STEP")
            selectors = [
                ("xpath", "//button[contains(text(),'GitHub')]"),
                ("xpath", "//a[contains(text(),'GitHub')]"),
                ("css", "[data-provider='github']"),
                ("xpath", "//*[contains(text(),'GitHub')]")
            ]
            if not self.find_and_click(selectors, "GitHub 登录"):
                self.log("找不到 GitHub 按钮", "ERROR")
                self.send_notification(False, "找不到 GitHub 登录按钮")
                sys.exit(1)

            time.sleep(3)
            self.capture_screenshot("点击后")
            url = self.driver.current_url
            self.log(f"当前: {url}")

            if 'signin' not in url.lower() and 'claw.cloud' in url and 'github.com' not in url:
                self.log("已登录！", "SUCCESS")
                self.detect_region(url)
                self.perform_keepalive()
                new_cookie = self.get_github_cookie()
                if new_cookie:
                    self.save_cookie_to_env(new_cookie)
                self.send_notification(True)
                print("\n✅ 成功！\n")
                return

            self.log("步骤 3: GitHub 认证", "STEP")
            
            if 'github.com/login' in url or 'github.com/session' in url:
                if not self.login_to_github():
                    self.capture_screenshot("登录失败")
                    self.send_notification(False, "GitHub 登录失败")
                    sys.exit(1)
            elif 'github.com/login/oauth/authorize' in url:
                self.log("Cookie 有效", "SUCCESS")
                self.handle_oauth_authorization()

            self.log("步骤 4: 等待重定向", "STEP")
            if not self.wait_redirect():
                self.capture_screenshot("重定向失败")
                self.send_notification(False, "重定向失败")
                sys.exit(1)
            
            self.capture_screenshot("重定向成功")

            self.log("步骤 5: 验证登录结果", "STEP")
            current_url = self.driver.current_url
            self.log(f"验证 URL: {current_url}")
            
            if 'claw.cloud' not in current_url or 'signin' in current_url.lower():
                self.send_notification(False, "验证失败")
                sys.exit(1)
            
            if not self.detected_region:
                self.detect_region(current_url)
            
            self.log("登录验证成功！", "SUCCESS")
            self.login_verified = True

            self.perform_keepalive()

            self.log("步骤 6: 更新 Cookie", "STEP")
            if self.new_cookie:
                self.save_cookie_to_env(self.new_cookie)
            else:
                self.driver.get("https://github.com")
                time.sleep(2)
                cookie = self.get_github_cookie()
                if cookie:
                    self.save_cookie_to_env(cookie)
                else:
                    self.log("未获取到新 Cookie", "WARN")

            self.send_notification(True)
            print("\n" + "=" * 60)
            print("✅ 成功！")
            if self.detected_region:
                print(f"📍 区域: {self.detected_region}")
            print("=" * 60 + "\n")

        except KeyboardInterrupt:
            self.log("用户中断", "WARN")
            self.send_notification(False, "用户中断")
            sys.exit(1)
        except Exception as e:
            self.log(f"异常: {e}", "ERROR")
            self.capture_screenshot("异常")
            import traceback
            traceback.print_exc()
            self.send_notification(False, str(e))
            sys.exit(1)
        finally:
            self.cleanup_resources()


if __name__ == "__main__":
    ClawCloudAutoLogin().execute_login_flow()
