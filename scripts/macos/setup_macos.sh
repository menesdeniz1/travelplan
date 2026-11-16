#!/usr/bin/env bash
# TravelPlan - macOS kurulum betiği. Tekrar çalıştırmak güvenlidir (idempotent).
#
# Yapar:
#   1. python3 >= 3.10 bulur (yoksa brew install python@3.12 önerir)
#   2. <repo>/.venv sanal ortamını oluşturur; çalışan bir tane varsa onu kullanır
#   3. requirements.txt paketlerini kurar
#   4. Playwright Chromium tarayıcısını kurar (zaten kuruluysa atlanır)
#   5. logs/ klasörünü oluşturur
#
# Kullanım:  scripts/macos/setup_macos.sh [--help]
# Sudo gerekmez. Sistem ayarlarına dokunmaz.

set -euo pipefail

usage() {
  cat <<'EOF'
TravelPlan macOS kurulumu

Kullanım:
  scripts/macos/setup_macos.sh [--help]

Ne yapar:
  - python3 (>= 3.10) bulur. Yoksa:  brew install python@3.12
  - <repo>/.venv sanal ortamını oluşturur (varsa ve çalışıyorsa yeniden kullanır)
  - requirements.txt paketlerini kurar
  - Playwright Chromium tarayıcısını kurar (zaten kuruluysa hızlıca atlanır)
  - logs/ klasörünü oluşturur

Sudo gerekmez. Betiği tekrar çalıştırmak güvenlidir.
Kurulumdan sonra otomatik başlatma için: scripts/macos/autostart_macos.sh install
EOF
}

die() {
  echo "HATA: $*" >&2
  exit 1
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    echo "Bilinmeyen seçenek: $1" >&2
    usage >&2
    exit 2
    ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  die "Bu betik yalnızca macOS için yazıldı."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
LOG_DIR="$REPO_DIR/logs"

if [[ ! -f "$REPO_DIR/server_v2.py" || ! -f "$REPO_DIR/requirements.txt" ]]; then
  die "Repo kökü bulunamadı ($REPO_DIR). server_v2.py ve requirements.txt aranıyor."
fi

# python3 >= 3.10 olan ilk adayı yazdırır. Bulamazsa 1 döner.
find_python() {
  local candidate resolved
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 \
                   /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    [[ -n "$resolved" ]] || continue
    if "$resolved" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      echo "$resolved"
      return 0
    fi
  done
  return 1
}

echo "==> Python kontrolü"
if ! PYTHON_BIN="$(find_python)"; then
  echo "HATA: Python 3.10 veya üstü bulunamadı." >&2
  echo "  Homebrew ile kurmak için:  brew install python@3.12" >&2
  echo "  Homebrew yoksa: https://www.python.org/downloads/macos/" >&2
  echo "  (macOS'un eski sistem python3'ü 3.9 olabilir; onu kullanmayın.)" >&2
  exit 1
fi
PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
echo "Python: $PYTHON_BIN ($PY_VERSION)"

echo "==> Sanal ortam ($VENV_DIR)"
if [[ -x "$VENV_PY" ]] && "$VENV_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  echo "Mevcut sanal ortam kullanılıyor."
else
  if [[ -e "$VENV_DIR" ]]; then
    echo "Bozuk veya eski sanal ortam yeniden oluşturuluyor."
    rm -rf "$VENV_DIR"
  fi
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  echo "Sanal ortam oluşturuldu."
fi

echo "==> Python paketleri (requirements.txt)"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install -r "$REPO_DIR/requirements.txt"

echo "==> Playwright Chromium (ilk seferde indirir, sonra atlanır)"
"$VENV_PY" -m playwright install chromium

echo "==> logs/ klasörü"
mkdir -p "$LOG_DIR"

# İlk kurulumda takipleri ve fiyat geçmişini başlangıç kopyasından al. Var olan DB'ye asla dokunmaz.
SEED_DB="$REPO_DIR/deploy/travelplan.seed.db"
if [[ ! -e "$REPO_DIR/travelplan.db" && -f "$SEED_DB" ]]; then
  echo "==> travelplan.db başlangıç kopyasından oluşturuluyor"
  cp "$SEED_DB" "$REPO_DIR/travelplan.db"
fi
if [[ ! -f "$REPO_DIR/.env" ]]; then
  echo "UYARI: .env yok. Telegram için: cp .env.example .env ve TELEGRAM_* satırlarını doldurun." >&2
fi

echo
echo "Kurulum tamam."
echo "Sonraki adım: scripts/macos/autostart_macos.sh install"
echo "(Betik çalıştırılamıyorsa:  chmod +x scripts/macos/*.sh )"
