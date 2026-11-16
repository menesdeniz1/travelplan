# scripts/

Sunucuyu çalıştırmak ve bilgisayar açılınca otomatik başlatmak için yardımcı dosyalar.

- Windows: `run_server.bat`, `autostart_windows.ps1` (aşağıda).
- macOS: `macos/` klasörü, sona bakın: [macOS](#macos).

## .env: ayarlar ve Telegram

Ayarları ve Telegram bilgilerini repo kökündeki `.env` dosyasında tutabilirsiniz. Sunucu
başlangıçta bu dosyayı kendisi okur; Windows görevine ya da macOS plist'ine sır yazmanız gerekmez.

1. Örnek dosyayı kopyalayın: `copy .env.example .env` (macOS/Linux: `cp .env.example .env`).
2. `.env` içinde `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` satırlarını doldurun.
3. Sunucuyu yeniden başlatın.

`.env` git'e eklenmez (`.gitignore` bunu engeller); gerçek değerleri yalnızca kendi bilgisayarınızda tutun.
Gerçek ortam değişkeni (kabuk, `setx`, launchd) `.env`'den önce gelir. Boş bırakılan satırlar yok sayılır.
Başka bir dosya kullanmak için `TRAVELPLAN_ENV_FILE` ortam değişkenine yolunu yazın.

## run_server.bat: elle başlatma

Çift tıklayın veya bir terminalden çalıştırın:

    scripts\run_server.bat

Sunucu bu pencerede çalışır ve adresi gösterir (varsayılan: http://127.0.0.1:8085).
Durdurmak için Ctrl+C. Repo kökü, script nereden çağrılırsa çağrılsın otomatik bulunur.

## autostart_windows.ps1: oturum açılınca otomatik başlatma

Zamanlanmış görev "TravelPlanServer" oluşturur. Görev, Windows'a giriş yaptığınızda
sunucuyu pencere açmadan (pythonw.exe ile) başlatır. Yönetici yetkisi gerekmez.

Terminalden (PowerShell veya cmd) çalıştırın:

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autostart_windows.ps1 -Status
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autostart_windows.ps1 -Install
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autostart_windows.ps1 -Uninstall

- `-Status`: görevin kayıtlı olup olmadığını, son çalışma zamanını ve sunucu portunun (varsayılan 8085) durumunu gösterir.
- `-Install`: görevi kaydeder. Mevcut görev varsa üzerine yazar.
- `-Uninstall`: görevi siler. Çalışan sunucuyu kapatmaz.
- `-WhatIf` (`-Install` veya `-Uninstall` ile): hiçbir şey değiştirmeden ne yapılacağını yazar.

Görev, kurulduğu anda çalıştırılacak programı ve argümanları kaydeder. Bu nedenle
önceden kurulmuş bir görev eski komutla (cmd.exe ve çıktı yönlendirmesiyle) çalışmaya devam eder.
Yeni ayarların geçerli olması için `-Install` komutunu yeniden çalıştırın; görev üzerine yazılır.

Kurulumdan sonra görevi hemen başlatmak için:

    Start-ScheduledTask -TaskName TravelPlanServer

### Günlük (log) dosyası

Görev, `pythonw.exe` ile doğrudan çalışır; cmd sarmalayıcısı ve çıktı yönlendirmesi (`>`) yoktur.
Sunucu günlüğünü kendisi `logs\server.log` dosyasına yazar ve kendisi döndürür:
dosya 5 MB'a ulaşınca `server.log.1`, `.2`, `.3` olarak kaydırılır (en fazla 3 yedek).
Dış bir yönlendirme dosyayı sürekli açık tutacağı için döndürme çalışmaz; bu yüzden
yönlendirme kullanılmaz. `logs/` klasörü `.gitignore` içinde zaten var.

Pencere olmadığı için görev günlüğü yalnızca dosyaya yazılır. Elle `run_server.bat` ile
başlatırsanız aynı günlük dosyasına ek olarak konsola da yazılır.

### Ortam değişkenleri

Sunucu bu değişkenleri başlangıçta okur. Görev bunları kendisi kaydetmez: görev
oturum açıldığında Windows kullanıcı ortam değişkenlerini kullanır. Değişkeni kalıcı
ayarlayıp (ör. `setx TRAVELPLAN_PORT 8090`) oturumu kapatıp yeniden açın. Ardından
`-Status` ve sunucu aynı değeri görür. Değişkenlerin hepsi isteğe bağlıdır:

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `TRAVELPLAN_HOST` | `127.0.0.1` | Dinlenecek adres. Yerel ağa açmak için bilerek değiştirin. |
| `TRAVELPLAN_PORT` | `8085` | HTTP portu. |
| `TRAVELPLAN_MAX_BROWSERS` | `2` (en az 1) | Uçuş aramasında aynı anda açık tarayıcı sayfası üst sınırı. |
| `TRAVELPLAN_BROWSER_IDLE_SECS` | `300` | Son aramadan sonra paylaşılan tarayıcının kapatılmasına kadar geçen saniye. |
| `TRAVELPLAN_BROWSER_REUSE` | `1` | `0` yaparsanız paylaşılan tarayıcı kapanır, her arama yeni tarayıcı açar. |
| `TRAVELPLAN_EUR_TRY` | (canlı kur) | Sabit EUR/TRY kuru. Ayarlıysa canlı kur yerine bu değer kullanılır. |
| `TRAVELPLAN_DB` | proje klasöründeki `travelplan.db` | Takip, fiyat geçmişi ve uyarı veritabanının yolu. |
| `TRAVELPLAN_SEARCH_LOG` | proje klasöründeki `search_log.db` | Arama günlüğü veritabanının yolu. |
| `TRAVELPLAN_SCAN_INTERVAL` | `21600` (6 saat) | Takip tarama aralığı, saniye (en az 900). Her tarama takip başına Google sayfası harcar; kısa aralık engellenme riskini artırır. `.env` içine de yazılabilir. |

### Notlar

- Görev, `pythonw.exe` bulunamazsa `python.exe` ile çalışır; bu durumda konsol penceresi açılabilir.
- Microsoft Store'daki Python takma adı (`WindowsApps` altındaki `python.exe`) kullanılmaz; gerçek bir Python kurulumu gerekir.
- `-Status`, `TRAVELPLAN_PORT` değerini komutu çalıştırdığınız ortamdan okuyarak portu kontrol eder. Sunucunun gerçek portu, oturum açılışında okunan değerdir.

## macOS

Bu bölüm TravelPlan'ı bir Mac'te (ör. ev sunucusu olarak kullanılan Mac mini) sürekli açık çalıştırmak içindir.
Dosyalar `scripts/macos/` altındadır:

| Dosya | Görevi |
|---|---|
| `setup_macos.sh` | Python, `.venv` sanal ortamı, paketler, Playwright Chromium. Tekrar çalıştırmak güvenlidir. |
| `autostart_macos.sh` | Kullanıcı LaunchAgent'ını kurar, başlatır, durdurur, durumunu ve günlüklerini gösterir, yeniden başlatır. |
| `com.travelplan.server.plist.template` | LaunchAgent şablonu. Elle düzenlemeyin; ayarlar `install` ile geçer. |

Sudo gerekmez. Hiçbir betik sistem ayarını değiştirmez.

### Gereksinimler

- macOS (Apple Silicon veya Intel).
- Python 3.10 veya üstü. Yoksa: `brew install python@3.12`. macOS ile gelen `python3` (3.9) yetmez.
- Proje klasörü Masaüstü, Belgeler veya İndirilenler altında olmamalı (aşağıdaki "Dikkat edilecekler").

### 1. Kurulum

```bash
cd ~/travelplan                      # proje klasörü
chmod +x scripts/macos/*.sh          # Windows'tan kopyaladıysanız bir kez
scripts/macos/setup_macos.sh
scripts/macos/setup_macos.sh --help  # seçenekler
```

Betik sırayla şunları yapar: Python 3.10+ bulur, `.venv` sanal ortamını oluşturur (varsa ve çalışıyorsa yeniden kullanır),
`requirements.txt` paketlerini kurar, `playwright install chromium` çalıştırır (Chromium zaten kuruluysa atlanır) ve `logs/` klasörünü oluşturur.

### 2. Otomatik başlatma (install)

```bash
scripts/macos/autostart_macos.sh install                 # yalnızca bu Mac: 127.0.0.1:8085
scripts/macos/autostart_macos.sh install --lan           # ev ağındaki cihazlardan erişim (bkz. LAN erişimi)
scripts/macos/autostart_macos.sh install --dry-run       # hiçbir şey yazmadan plist'i ve komutları gösterir
```

`install`:

- `~/Library/LaunchAgents/com.travelplan.server.plist` dosyasını yazar (şablondan, yer tutucular doldurularak).
- `launchctl bootstrap gui/$UID` ile yükler; sunucu hemen başlar.
- `RunAtLoad`: oturum açılınca başlar. `KeepAlive`: çökerse yeniden başlatır. `ThrottleInterval` 30 sn'dir; sunucu sürekli çöküyorsa 30 sn'den sık denenmez.
- Çalışma dizini proje klasörüdür. launchd çıktıları `logs/launchd.out.log` ve `logs/launchd.err.log` dosyalarına gider.
- Aynı komut tekrar çalıştırılabilir: eski kaydı kaldırıp yenisini yazar.

Seçenekler ve ortam değişkenleri:

| Seçenek / değişken | Varsayılan | Anlamı |
|---|---|---|
| `--host ADRES` / `TRAVELPLAN_HOST` | `127.0.0.1` | Dinlenecek adres: `localhost`, `127.0.0.1` veya IPv4 LAN adresi. `0.0.0.0` kabul edilmez. |
| `--lan` | kapalı | Mac'in LAN IP'sini `ipconfig getifaddr en0` / `en1` ile bulur ve `--host` yerine kullanır. Mac'in Bonjour adını (`scutil --get LocalHostName` + `.local`) da kabul edilen Host adlarına ekler. |
| `--allowed-hosts LISTE` / `TRAVELPLAN_ALLOWED_HOSTS` | boş | Sunucunun ek kabul ettiği Host/Origin adları, virgülle ayrılmış (ör. `mac.local,192.168.1.20`). Yalnızca ad veya IPv4; port, şema ve `*` kabul edilmez. Seçenek ortam değişkenine göre önceliklidir. |
| `--port N` / `TRAVELPLAN_PORT` | `8085` | HTTP portu. |
| `--max-browsers N` / `TRAVELPLAN_MAX_BROWSERS` | `2` | Aynı anda açık Chromium sayfası üst sınırı (en az 1). |
| `TRAVELPLAN_TRANSITOUS` | kapalı | Yalnızca `1` ise Transitous açılır (plist'e eklenir). Kullanım koşulları gereği Transitous ekibine bildirmeden açmayın. |
| `--dry-run` | kapalı | `install`, `uninstall`, `restart` için: yazmadan önizleme. |

Ortam değişkenleri `install` anında plist'e yazılır. Bir değeri değiştirmek için `install`'ı seçeneklerle yeniden çalıştırın.

### 3. Durum, günlük, yeniden başlatma

```bash
scripts/macos/autostart_macos.sh status        # kayıt, yüklü mü, PID, son çıkış kodu, /api/health yanıtı
scripts/macos/autostart_macos.sh logs          # logs/server.log ve launchd.err.log son 80 satır
scripts/macos/autostart_macos.sh logs -f       # canlı izleme (Ctrl+C ile çıkın)
scripts/macos/autostart_macos.sh restart       # sunucuyu yeniden başlatır
```

Günlük dosyaları:

- `logs/server.log`: asıl günlük. Sunucu kendisi döndürür (5 MB, 3 yedek).
- `logs/launchd.err.log`, `logs/launchd.out.log`: launchd'nin yakaladığı çıktı. Sunucu stderr bir terminal değilse konsola ayrıca yazmaz (`TRAVELPLAN_LOG_CONSOLE=1` ile zorlanabilir), bu yüzden `server.log` ile aynı satırları burada tekrarlamaz; buraya yalnızca kütüphanelerin stderr'e yazdığı çıktı düşer. Dosyalar döndürülmez. `status` 20 MB'ı geçerse uyarır. O zaman `rm logs/launchd.err.log` ardından `scripts/macos/autostart_macos.sh restart` çalıştırın.

### 4. Güncelleme

> **Bir kerelik: `.env` ve `deploy/travelplan.seed.db` git takibinden çıkarıldıktan sonraki ilk `git pull`.**
> Bu çekme, Mac'teki `.env` dosyasını siler (dosya artık repoda yok). Önce yedekleyin, sonra geri koyun:
>
> ```bash
> cd ~/travelplan
> cp .env ~/travelplan.env.yedek
> git checkout -- .env            # yerel değişiklik varsa çekmeyi engellemesin (yedek alındı)
> git pull
> cp ~/travelplan.env.yedek .env
> scripts/macos/autostart_macos.sh restart
> ```
>
> `travelplan.db` etkilenmez. Yeni bir Mac kurulumunda başlangıç veritabanını elle kopyalayın (ör. `scp`), repoda artık yok.

```bash
cd ~/travelplan
git pull
scripts/macos/setup_macos.sh                 # requirements.txt değiştiyse paketleri günceller (tekrar güvenli)
scripts/macos/autostart_macos.sh restart     # yeni kodu yükler
```

`restart` yalnızca servisi yeniden başlatır; paket kurmaz. Bu yüzden `git pull` sonrası `setup_macos.sh` adımı önerilir.

### 5. Kaldırma

```bash
scripts/macos/autostart_macos.sh uninstall
```

Plist dosyasını siler ve çalışan sunucuyu durdurur. Proje, `travelplan.db`, `config.json` ve `logs/` yerinde kalır.
`.venv` ve Playwright önbelleği de kalır; isterseniz elle silebilirsiniz.

### LAN erişimi

Varsayılan adres `127.0.0.1`: sunucuya yalnızca Mac'in kendisinden erişilir. Ev ağındaki telefon ve bilgisayarlardan erişmek için:

```bash
scripts/macos/autostart_macos.sh install --lan
```

Diğer cihazdan `http://<Mac'in LAN IP'si>:8085` adresini açın (ör. `http://192.168.1.20:8085`). **Mac'in kendisinde de aynı adresi kullanın:** sunucu yalnızca bu IP'de dinlediği için `127.0.0.1` artık yanıt vermez.

**Neden `0.0.0.0` değil?** Sunucu (`server_v2.py`) gelen `Host` başlığını kontrol eder (DNS rebinding koruması). Kabul edilen değerler `localhost`, `127.0.0.1` ve `TRAVELPLAN_HOST`'tur. `TRAVELPLAN_HOST=0.0.0.0` ile sunucu tüm arayüzlerde dinler, ama LAN'dan gelen `Host: 192.168.1.20:8085` isteği eşleşmediği için **403 "Geçersiz Host başlığı"** ile reddedilir. Bu yüzden betik `0.0.0.0` değerini reddeder. Doğru ayar Mac'in LAN IP'sidir. `install --lan` ayrıca Mac'in Bonjour adını (ör. `mac-mini.local`) kabul edilen adlara ekler; başka bir ad (ör. bir DNS adı) için `--allowed-hosts ad.local` verin. Ad, IP'nin yerine geçen bir kolaylıktır: IP her zaman çalışır, ad ise Mac'in adı çözülebilen ağlarda kullanılabilir.

Notlar:

- **IP sabit olsun.** Router'da Mac için DHCP rezervasyonu tanımlayın. IP değişirse `status` uyarır; `install --lan` ile yeniden kurun. Eski IP Mac'te yoksa sunucu bağlanamaz (bind hatası) ve launchd 30 sn'de bir yeniden dener.
- **Güvenlik duvarı.** macOS güvenlik duvarı açıksa Python için bağlantı izni sorabilir. Sistem Ayarları > Ağ > Güvenlik Duvarı üzerinden izin verin.
- **Kimlik doğrulama yok.** Ev ağındaki herkes arayüze ve API'ye erişir. Yalnızca güvenilir ev ağında açın. Router'da port yönlendirmesi (port forwarding) yapmayın ve sunucuyu internete açmayın.
- **Uzaktan kullanım için Telegram.** Telegram botu giden (outbound) uzun sorgu ile çalışır; Mac'e gelen bağlantı açmaz. Uzaktan arama ve takip için bunu kullanın.

### Dikkat edilecekler

- **Uyku.** Mac uyuduğunda sunucu durur. Sistem Ayarları > Enerji bölümünde Mac'in uyumasını engelleyin. Terminalden (yönetici yetkisi ister): `sudo pmset -a sleep 0`.
- **Oturum.** LaunchAgent kullanıcı oturumu açıldığında çalışır. Mac yeniden başlarken oturum açılmazsa (FileVault açıkken parola istenir) sunucu başlamaz. Bu betik oturum açma ayarlarına dokunmaz.
- **Proje klasörü.** Masaüstü, Belgeler veya İndirilenler altında olmasın; launchd ile başlayan Python bu klasörlere erişemeyebilir ("Operation not permitted"). `install` bu durumda uyarır. Örnek: `~/travelplan`.
- **Satır sonları.** `.sh` dosyaları LF satır sonlu olmalıdır. Windows'ta `core.autocrlf=true` ile klonlanırsa CRLF olabilir ve betik `$'\r': command not found` hatası verir. Düzeltme (macOS): `sed -i '' 's/\r$//' scripts/macos/*.sh`.
- **Veri Mac'e taşınmaz.** `travelplan.db`, `config.json` ve `logs/` Windows makinesinden otomatik kopyalanmaz. Telegram ayarlarını Mac'teki arayüzden yeniden girin veya dosyaları elle kopyalayın.
- **Çalıştırma izni.** `setup_macos.sh` ve `autostart_macos.sh` çalıştırma izni (`chmod +x`) ister. Git bu izni Windows'tan commit edemez; Mac'te bir kez `chmod +x` çalıştırın.

### Sorun giderme

| Belirti | Bakılacak yer |
|---|---|
| `Python 3.10 veya üstü bulunamadı` | `brew install python@3.12`, sonra `setup_macos.sh`. |
| `status`: "Yüklü     : hayır" | `install` çalıştırılmamış ya da `uninstall` yapılmış. |
| `status`: "HTTP      : yanıt yok" ve son çıkış kodu sıfırdan farklı | `logs` ile `logs/launchd.err.log` ve `logs/server.log` sonuna bakın. Düzeltmeden sonra `restart`. |
| `Operation not permitted` | Proje klasörü Masaüstü/Belgeler/İndirilenler altında; taşıyın. |
| `Can't assign requested address` | Plist'teki LAN IP Mac'te yok (DHCP değişti). `install --lan`. |
| 403 "Geçersiz Host başlığı" | Adres çubuğundaki ad, `localhost`, `127.0.0.1`, `--host` değeri veya `--allowed-hosts` listesinde yok. `install --lan` ile Bonjour adını ekleyin, ya da IP kullanın. |
| Arama boş, Chromium hatası | `setup_macos.sh` (Chromium adımı) ya da `.venv/bin/python -m playwright install chromium`. |
