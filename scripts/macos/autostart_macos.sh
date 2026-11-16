#!/usr/bin/env bash
# TravelPlan - macOS otomatik başlatma. Kullanıcı LaunchAgent'ı kurar; sudo gerekmez.
#
# Komutlar:
#   install     plist'i ~/Library/LaunchAgents altına yazar ve yükler (oturum açılınca başlar, çökerse yeniden başlatır)
#   uninstall   LaunchAgent'ı kaldırır (çalışan sunucuyu durdurur)
#   status      Kayıt, çalışma durumu, son çıkış kodu, HTTP sağlık kontrolü
#   logs [-f]   Sunucu ve launchd günlüklerinin son satırları (-f: canlı izle)
#   restart     Sunucuyu yeniden başlatır (git pull sonrası kullanın)
#
# install seçenekleri (ortam değişkenleri de okunur; seçenek öncelikli):
#   --host ADRES       TRAVELPLAN_HOST: 127.0.0.1 (varsayılan), localhost veya Mac'in LAN IP'si
#   --lan              Mac'in LAN IP'sini (en0/en1) otomatik bulur ve --host yerine kullanır;
#                      Mac'in Bonjour adını (scutil --get LocalHostName).local listeye ekler
#   --allowed-hosts L  TRAVELPLAN_ALLOWED_HOSTS: virgüllü ek ad listesi (ör. mac.local,192.168.1.20)
#   --port N           TRAVELPLAN_PORT (varsayılan 8085)
#   --max-browsers N   TRAVELPLAN_MAX_BROWSERS (varsayılan 2)
#   --dry-run          Hiçbir şey değiştirmeden plist'i ve komutları yazdırır (install, uninstall, restart)
#
# Ortam değişkenleri plist'e install anında yazılır. Değiştirmek için install'ı yeniden çalıştırın.
# Sunucu repo kökündeki <repo>/.env dosyasını başlangıçta kendisi okur (travel_engine/envfile.py).
# Bu yüzden TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID gibi sırlar plist'e yazılmaz; .env.example'ı
# .env adıyla kopyalayıp doldurmanız yeterlidir. plist'te veya kabukta ayarlı bir değişken .env'den önce gelir.
# TRAVELPLAN_TRANSITOUS ayarlıysa plist'e eklenir (sunucu bu değişkeni okur; Transitous yalnızca bu değişkenle açılır).

set -euo pipefail
# bash 5.2+: ${x//pat/$rep} içinde & eşleşmeyi anlatır; kapat. macOS bash 3.2 bu ayarı bilmez.
shopt -u patsub_replacement 2>/dev/null || true

LABEL="com.travelplan.server"
SELF="scripts/macos/autostart_macos.sh"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
SERVICE="$DOMAIN/$LABEL"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.travelplan.server.plist.template"
VENV_PY="$REPO_DIR/.venv/bin/python"
LOG_DIR="$REPO_DIR/logs"

usage() {
  cat <<EOF
TravelPlan macOS otomatik başlatma

Kullanım:
  $SELF install [--host ADRES | --lan] [--allowed-hosts LISTE] [--port N] [--max-browsers N] [--dry-run]
  $SELF uninstall [--dry-run]
  $SELF status
  $SELF logs [-f]
  $SELF restart [--dry-run]

Komutlar:
  install    LaunchAgent'ı yazar ve yükler. Sunucu hemen başlar, oturum açılınca tekrar başlar.
  uninstall  LaunchAgent'ı kaldırır. Çalışan sunucuyu durdurur.
  status     Kurulu mu, çalışıyor mu, son çıkış kodu, /api/health yanıtı.
  logs       logs/server.log ve logs/launchd.err.log son satırları. -f ile canlı izler.
  restart    Sunucuyu yeniden başlatır. Kodu git pull ile güncelledikten sonra kullanın.

Seçenekler:
  --host ADRES       TRAVELPLAN_HOST. 127.0.0.1 (varsayılan), localhost veya LAN IP'si (ör. 192.168.1.20).
                     0.0.0.0 kabul edilmez; sunucunun Host kontrolü LAN isteklerini reddeder.
  --lan              Mac'in LAN IP'sini otomatik bulur (en0, sonra en1). Mac'in Bonjour adını
                     (ör. mac-mini.local) da kabul edilen adlara ekler.
  --allowed-hosts L  TRAVELPLAN_ALLOWED_HOSTS. Virgülle ayrılmış ek ad veya IPv4 listesi, ör.
                     "mac.local,192.168.1.20". Port, şema ve joker (*) kabul edilmez.
                     Ayrıca ortam değişkeni TRAVELPLAN_ALLOWED_HOSTS okunur; seçenek öncelikli.
  --port N           Port (varsayılan 8085).
  --max-browsers N   Aynı anda açık Chromium sayfası üst sınırı (varsayılan 2).
  --dry-run          Plist'i ve yapılacak komutları yazdırır, hiçbir şey değiştirmez.
  -h, --help         Bu yardım.
EOF
}

die() {
  echo "HATA: $*" >&2
  exit 1
}

warn() {
  echo "UYARI: $*" >&2
}

COMMAND=""
DRY_RUN=0
LAN=0
FOLLOW=0
OPT_HOST=""
OPT_PORT=""
OPT_MAX_BROWSERS=""
OPT_ALLOWED_HOSTS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|uninstall|status|logs|restart)
      COMMAND="$1"
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --lan)
      LAN=1
      ;;
    -f|--follow)
      FOLLOW=1
      ;;
    --host|--port|--max-browsers|--allowed-hosts)
      [[ $# -ge 2 ]] || die "$1 bir değer ister."
      case "$1" in
        --host) OPT_HOST="$2" ;;
        --port) OPT_PORT="$2" ;;
        --allowed-hosts) OPT_ALLOWED_HOSTS="$2" ;;
        *) OPT_MAX_BROWSERS="$2" ;;
      esac
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Bilinmeyen seçenek: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -z "$COMMAND" ]]; then
  usage >&2
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" && $DRY_RUN -eq 0 ]]; then
  die "Bu betik yalnızca macOS içindir. Önizleme için --dry-run kullanabilirsiniz."
fi

is_ipv4() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  local octet
  for octet in "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"; do
    (( 10#$octet <= 255 )) || return 1
  done
  return 0
}

is_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 ))
}

is_positive_int() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 ))
}

# En iyi çaba: en0 ve en1 üzerindeki IPv4 adresini bulur.
detect_lan_ip() {
  local iface ip
  [[ "$(uname -s)" == "Darwin" ]] || return 1
  for iface in en0 en1; do
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    if [[ -n "$ip" ]]; then
      echo "$ip"
      return 0
    fi
  done
  return 1
}

# Mac'in Bonjour adı (scutil --get LocalHostName) + ".local". Yoksa boş döner.
detect_local_hostname() {
  [[ "$(uname -s)" == "Darwin" ]] || return 1
  local name
  name="$(scutil --get LocalHostName 2>/dev/null || true)"
  [[ -n "$name" ]] || return 1
  echo "$name.local"
}

# Virgüllü listeyi küçük harfe çevirip doğrular, boşlukları ve tekrarları atar.
# Sunucunun kabul ettiği biçimle aynıdır: bare ad veya IPv4 (port, şema, joker yok).
normalize_allowed_hosts() {
  local raw="$1" item out="" seen=","
  local re='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$'
  while IFS= read -r item; do
    item="$(printf '%s' "$item" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    [[ -n "$item" ]] || continue
    [[ "$item" =~ $re ]] || die "Geçersiz ad --allowed-hosts / TRAVELPLAN_ALLOWED_HOSTS içinde: '$item' (yalnızca ad veya IPv4; port, şema ve * olmaz)."
    case "$seen" in
      *",$item,"*) continue ;;
    esac
    seen="$seen$item,"
    out="${out:+$out,}$item"
  done <<< "$(printf '%s' "$raw" | tr ',' '\n')"
  printf '%s' "$out"
}

# Ortam değişkenleri ve seçeneklerden kesin ayarları üretir.
resolve_settings() {
  local local_name=""
  if [[ $LAN -eq 1 ]]; then
    [[ -z "$OPT_HOST" ]] || die "--lan ile --host birlikte kullanılamaz."
    HOST_VAL="$(detect_lan_ip)" || die "LAN IP bulunamadı (en0/en1). --host ADRES ile elle verin."
    echo "LAN IP: $HOST_VAL"
    local_name="$(detect_local_hostname || true)"
    if [[ -n "$local_name" ]]; then
      echo "Yerel ad: $local_name (kabul edilen adlara eklenir)"
    else
      warn "Mac'in yerel adı bulunamadı; gerekirse --allowed-hosts ile ekleyin."
    fi
  else
    HOST_VAL="${OPT_HOST:-${TRAVELPLAN_HOST:-127.0.0.1}}"
  fi
  PORT_VAL="${OPT_PORT:-${TRAVELPLAN_PORT:-8085}}"
  MAX_VAL="${OPT_MAX_BROWSERS:-${TRAVELPLAN_MAX_BROWSERS:-2}}"
  TRANSITOUS_VAL="${TRAVELPLAN_TRANSITOUS:-}"
  ALLOWED_VAL="$(normalize_allowed_hosts "${OPT_ALLOWED_HOSTS:-${TRAVELPLAN_ALLOWED_HOSTS:-}}")"
  if [[ -n "$local_name" ]]; then
    ALLOWED_VAL="$(normalize_allowed_hosts "${ALLOWED_VAL:+$ALLOWED_VAL,}$local_name")"
  fi

  case "$HOST_VAL" in
    0.0.0.0|::|"")
      die "TRAVELPLAN_HOST=$HOST_VAL kabul edilmez. Sunucunun Host kontrolü yalnızca localhost, 127.0.0.1 ve TRAVELPLAN_HOST değerini kabul eder. LAN'dan erişmek için Mac'in LAN IP'sini kullanın (--lan)."
      ;;
    127.0.0.1|localhost)
      ;;
    *)
      is_ipv4 "$HOST_VAL" || die "Geçersiz adres: $HOST_VAL (IPv4 adresi, localhost veya 127.0.0.1 olmalı)."
      ;;
  esac
  is_port "$PORT_VAL" || die "Geçersiz port: $PORT_VAL"
  is_positive_int "$MAX_VAL" || die "Geçersiz --max-browsers değeri: $MAX_VAL (en az 1)"
}

# XML metin düğümü için kaçış (& < > " ').
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}

# Şablonu doldurup stdout'a yazar.
render_plist() {
  local repo_x python_x host_x port_x max_x allowed_x transitous_x line
  repo_x="$(xml_escape "$REPO_DIR")"
  python_x="$(xml_escape "$VENV_PY")"
  host_x="$(xml_escape "$HOST_VAL")"
  port_x="$(xml_escape "$PORT_VAL")"
  max_x="$(xml_escape "$MAX_VAL")"
  allowed_x="$(xml_escape "$ALLOWED_VAL")"
  transitous_x="$(xml_escape "$TRANSITOUS_VAL")"

  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "__OPTIONAL_ENV__" ]]; then
      if [[ -n "$TRANSITOUS_VAL" ]]; then
        printf '        <key>TRAVELPLAN_TRANSITOUS</key>\n'
        printf '        <string>%s</string>\n' "$transitous_x"
      fi
      continue
    fi
    # Tırnaksız: bash 3.2 iç tırnakları metne yazar. & için yukarıdaki shopt yeterli.
    line="${line//__REPO_DIR__/$repo_x}"
    line="${line//__PYTHON__/$python_x}"
    line="${line//__HOST__/$host_x}"
    line="${line//__PORT__/$port_x}"
    line="${line//__MAX_BROWSERS__/$max_x}"
    line="${line//__ALLOWED_HOSTS__/$allowed_x}"
    printf '%s\n' "$line"
  done < "$TEMPLATE"
}

warn_if_protected_folder() {
  case "$REPO_DIR" in
    "$HOME/Desktop"|"$HOME/Desktop/"*|"$HOME/Documents"|"$HOME/Documents/"*|"$HOME/Downloads"|"$HOME/Downloads/"*)
      warn "Proje Masaüstü, Belgeler veya İndirilenler altında. launchd ile başlayan Python bu klasörlere"
      warn "erişemeyebilir (\"Operation not permitted\"). Sorun çıkarsa projeyi ~/travelplan gibi bir klasöre taşıyın."
      ;;
  esac
}

plist_value() {
  /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$1" "$PLIST_PATH" 2>/dev/null || true
}

cmd_install() {
  [[ -f "$REPO_DIR/server_v2.py" ]] || die "server_v2.py bulunamadı: $REPO_DIR"
  [[ -f "$TEMPLATE" ]] || die "Plist şablonu bulunamadı: $TEMPLATE"
  if [[ ! -x "$VENV_PY" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      warn "Sanal ortam yok ($VENV_PY). Gerçek kurulumdan önce: scripts/macos/setup_macos.sh"
    else
      die "Sanal ortam bulunamadı ($VENV_PY). Önce: scripts/macos/setup_macos.sh"
    fi
  fi
  resolve_settings
  warn_if_protected_folder

  if [[ "$HOST_VAL" != "127.0.0.1" && "$HOST_VAL" != "localhost" ]]; then
    echo "Not: Sunucu $HOST_VAL adresinde dinleyecek. Bu adrese 127.0.0.1 ile bağlanılmaz;"
    echo "     Mac'in kendisinde de http://$HOST_VAL:$PORT_VAL kullanın. Yalnızca güvenilir ev ağında açın."
  fi
  if [[ -n "$ALLOWED_VAL" ]]; then
    echo "Ek kabul edilen Host adları: $ALLOWED_VAL"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    echo "----- $PLIST_PATH (önizleme) -----"
    render_plist
    echo "----- yapılacak komutlar (hiçbiri çalıştırılmadı) -----"
    echo "[dry-run] mkdir -p \"$LOG_DIR\" \"$(dirname "$PLIST_PATH")\""
    echo "[dry-run] launchctl bootout $SERVICE   (zaten yüklüyse; hata yok sayılır)"
    echo "[dry-run] plutil -lint \"$PLIST_PATH.tmp\""
    echo "[dry-run] mv \"$PLIST_PATH.tmp\" \"$PLIST_PATH\""
    echo "[dry-run] launchctl bootstrap $DOMAIN \"$PLIST_PATH\""
    return 0
  fi

  mkdir -p "$LOG_DIR" "$(dirname "$PLIST_PATH")"

  # Eski kayıt yüklüyse önce kaldır; yoksa bootstrap hata verir.
  launchctl bootout "$SERVICE" >/dev/null 2>&1 || true

  local tmp="$PLIST_PATH.tmp"
  render_plist > "$tmp"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$tmp" >/dev/null || { rm -f "$tmp"; die "Üretilen plist geçersiz. Ayarları kontrol edin."; }
  fi
  mv "$tmp" "$PLIST_PATH"
  chmod 644 "$PLIST_PATH"

  launchctl bootstrap "$DOMAIN" "$PLIST_PATH" || die "launchctl bootstrap başarısız. '$SELF status' ve logs/launchd.err.log dosyasına bakın."

  echo "Kuruldu: $PLIST_PATH"
  echo "Sunucu başlatıldı. Adres: http://$HOST_VAL:$PORT_VAL"
  echo "Durum: $SELF status    Günlük: $SELF logs"
}

cmd_uninstall() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] launchctl bootout $SERVICE   (yüklü değilse hata yok sayılır)"
    echo "[dry-run] rm -f \"$PLIST_PATH\""
    return 0
  fi
  if launchctl print "$SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$SERVICE"
    echo "Servis durduruldu ve kaldırıldı."
  else
    echo "Servis zaten yüklü değil."
  fi
  if [[ -f "$PLIST_PATH" ]]; then
    rm -f "$PLIST_PATH"
    echo "Plist silindi: $PLIST_PATH"
  fi
  echo "Günlükler logs/ içinde kaldı."
}

cmd_status() {
  echo "LaunchAgent: $PLIST_PATH"
  if [[ -f "$PLIST_PATH" ]]; then
    echo "  Kayıtlı   : evet"
  else
    echo "  Kayıtlı   : hayır (kurmak için: $SELF install)"
  fi

  local info=""
  if info="$(launchctl print "$SERVICE" 2>/dev/null)"; then
    echo "  Yüklü     : evet"
    echo "  Durum     : $(awk -F' = ' '/^[[:space:]]*state = /{print $2; exit}' <<< "$info")"
    echo "  PID       : $(awk -F' = ' '/^[[:space:]]*pid = /{print $2; exit}' <<< "$info")"
    echo "  Son çıkış : $(awk -F' = ' '/^[[:space:]]*last exit code = /{print $2; exit}' <<< "$info")"
  else
    echo "  Yüklü     : hayır"
  fi

  if [[ -f "$PLIST_PATH" ]]; then
    local host port
    host="$(plist_value TRAVELPLAN_HOST)"
    port="$(plist_value TRAVELPLAN_PORT)"
    host="${host:-127.0.0.1}"
    port="${port:-8085}"
    echo "  Adres     : http://$host:$port"
    if curl -fsS -m 5 "http://$host:$port/api/health" >/dev/null 2>&1; then
      echo "  HTTP      : yanıt veriyor (/api/health)"
    else
      echo "  HTTP      : yanıt yok"
    fi
    if is_ipv4 "$host" && ! [[ "$(ifconfig)" == *"inet $host "* ]]; then
      warn "Plist'teki IP ($host) bu Mac'te görünmüyor. DHCP adresi değişmiş olabilir. '$SELF install --lan' ile yeniden kurun."
    fi
  fi

  echo "  Günlükler : $LOG_DIR"
  local size_kb
  for f in "$LOG_DIR/launchd.err.log" "$LOG_DIR/launchd.out.log"; do
    if [[ -f "$f" ]]; then
      size_kb="$(du -k "$f" | awk '{print $1}')"
      if (( size_kb > 20480 )); then
        warn "$(basename "$f") ${size_kb} KB. Bu dosya döndürülmez. Silip '$SELF restart' çalıştırabilirsiniz."
      fi
    fi
  done
}

cmd_logs() {
  if [[ $FOLLOW -eq 1 ]]; then
    echo "Canlı izleme (çıkmak için Ctrl+C)."
    exec tail -n 50 -F "$LOG_DIR/server.log" "$LOG_DIR/launchd.err.log"
  fi
  local f
  for f in "$LOG_DIR/server.log" "$LOG_DIR/launchd.err.log"; do
    echo "===== $f (son 80 satır) ====="
    if [[ -f "$f" ]]; then
      tail -n 80 "$f"
    else
      echo "(dosya yok)"
    fi
    echo
  done
}

cmd_restart() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] launchctl kickstart -k $SERVICE"
    return 0
  fi
  launchctl print "$SERVICE" >/dev/null 2>&1 || die "Servis yüklü değil. Önce: $SELF install"
  launchctl kickstart -k "$SERVICE"
  echo "Sunucu yeniden başlatıldı. Durum: $SELF status"
}

case "$COMMAND" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  restart)   cmd_restart ;;
esac
