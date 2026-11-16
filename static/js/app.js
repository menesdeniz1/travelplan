    const STORAGE_KEY = 'travelplan_session_v3';
    let currentTripType = 'roundtrip';
    let allFlights = [];
    let currentSort = 'price'; // 'price' | 'duration'
    let selectedStops = 'all'; // 'all' | 0 | 1 | 2
    let selectedMaxDuration = 'all'; // 'all' | 5 | 10 | 15 | 24
    let selectedTimeSlot = 'all'; // 'all' | 'morning' | 'afternoon' | 'evening'
    let selectedAirline = null;

    function setTripType(type) {
      currentTripType = type;
      document.getElementById('btnRoundtrip').classList.toggle('active', type === 'roundtrip');
      document.getElementById('btnOneway').classList.toggle('active', type === 'oneway');
      document.getElementById('retDateContainer').style.display = type === 'oneway' ? 'none' : 'block';
    }

    function setDest(city) {
      document.getElementById('destination').value = city;
      searchLiveFlights();
    }

    function clearSearch() {
      localStorage.removeItem(STORAGE_KEY);
      localStorage.removeItem('travelplan_last_search');
      localStorage.removeItem('travelplan_last_results');
      document.getElementById('destination').value = '';
      hideResultsUI();
      showEmptyState();
    }

    function hideResultsUI() {
      document.getElementById('summaryCardsContainer').style.display = 'none';
      document.getElementById('filterToolbar').style.display = 'none';
      const matrixEl = document.getElementById('dateMatrixContainer');
      if (matrixEl) matrixEl.style.display = 'none';
      const chartEl = document.getElementById('priceHistoryContainer');
      if (chartEl) chartEl.style.display = 'none';
      const mmEl = document.getElementById('multimodalSection');
      if (mmEl) mmEl.style.display = 'none';
    }

    function showEmptyState() {
      hideResultsUI();
      const listEl = document.getElementById('liveFlightsList');
      listEl.innerHTML = `
        <div style="text-align: center; padding: 60px 20px; background: var(--surface-bg); border: 1px dashed var(--card-border); border-radius: 14px;">
          <div style="font-size: 38px; margin-bottom: 12px;">🌍✈️</div>
          <div style="font-size: 16px; font-weight: 700; color: var(--text); margin-bottom: 6px;">Gitmek İstediğiniz Şehri Seçin</div>
          <div style="font-size: 13px; color: var(--text-muted); max-width: 480px; margin: 0 auto; line-height: 1.6;">
            Yukarıdaki varış kutusuna gitmek istediğiniz şehri yazıp <strong>Canlı Ara</strong>'ya basın veya hızlı seçim butonlarına tıklayın.
          </div>
        </div>
      `;
    }

    function escapeHtml(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    /* URL for href attributes: only http(s) survives, everything else becomes "#" */
    function safeUrl(u) {
      const s = (u === null || u === undefined) ? '' : String(u).trim();
      return /^https?:\/\//i.test(s) ? escapeHtml(s) : '#';
    }

    /* Value for an inline handler argument, e.g. onclick="fn(${jsArg(x)})".
       JSON-encodes for the JS context, then HTML-escapes for the attribute context. */
    function jsArg(v) {
      return escapeHtml(JSON.stringify((v === null || v === undefined) ? '' : String(v)));
    }

    /* ---- Multi-modal helpers ---- */
    const TR_MONTHS_SHORT = ['Oca', 'Şub', 'Mar', 'Nis', 'May', 'Haz', 'Tem', 'Ağu', 'Eyl', 'Eki', 'Kas', 'Ara'];
    const TR_DAYS_SHORT = ['Paz', 'Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt'];
    const TIME_VALUE_KEY = 'travelplan_time_value_try_per_hour';
    const TIME_VALUE_DEFAULT = 150;

    function hasNumber(v) {
      return typeof v === 'number' && Number.isFinite(v);
    }

    /* "YYYY-MM-DD" + offset days -> e.g. "29 Eki Per". Returns '' for an invalid date. UTC math avoids timezone drift. */
    function formatTrShortDate(isoDate, offsetDays) {
      const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || '').trim());
      if (!m) return '';
      const off = Number.isFinite(Number(offsetDays)) ? Math.trunc(Number(offsetDays)) : 0;
      const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3] + off));
      if (isNaN(d.getTime())) return '';
      return `${d.getUTCDate()} ${TR_MONTHS_SHORT[d.getUTCMonth()]} ${TR_DAYS_SHORT[d.getUTCDay()]}`;
    }

    /* minutes -> "X sa Y dk" */
    function formatSaDk(totalMins) {
      const n = Math.max(0, Math.round(Number(totalMins) || 0));
      return `${Math.floor(n / 60)} sa ${n % 60} dk`;
    }

    /* Savings line. Without a hotel estimate this is exactly the server's savings_try.
       With one, the comparison uses the true total: baseline (total_price_try + savings_try) minus true_total_try. */
    function routeSavingsTry(r) {
      // Backend computes savings_try against true_total_try (hotel estimate included)
      return Math.max(0, Number(r.savings_try) || 0);
    }

    function parseTimeValue(raw) {
      const n = parseFloat(raw);
      return (Number.isFinite(n) && n >= 0) ? n : TIME_VALUE_DEFAULT;
    }

    function readTimeValueSetting() {
      const el = document.getElementById('inputTimeValue');
      return parseTimeValue(el ? el.value : TIME_VALUE_DEFAULT);
    }

    function saveTimeValueSetting() {
      const el = document.getElementById('inputTimeValue');
      if (!el) return;
      const v = parseTimeValue(el.value);
      el.value = String(v);
      try {
        localStorage.setItem(TIME_VALUE_KEY, String(v));
      } catch (e) {
        // Storage unavailable (private mode, blocked): the value still applies for this page session.
      }
    }

    function initTimeValueSetting() {
      const el = document.getElementById('inputTimeValue');
      if (!el) return;
      let saved = null;
      try {
        saved = localStorage.getItem(TIME_VALUE_KEY);
      } catch (e) {
        saved = null;
      }
      el.value = String(saved === null ? TIME_VALUE_DEFAULT : parseTimeValue(saved));
    }

    function toggleMultiModalOptions() {
      const chk = document.getElementById('chkMultiModal');
      const row = document.getElementById('multimodalInputsRow');
      if (row) row.style.display = chk.checked ? 'flex' : 'none';
    }

    /* "HH:MM" from a "YYYY-MM-DDTHH:MM" engine timestamp ('' when missing) */
    function hhmmOf(iso) {
      const m = String(iso || '').match(/T(\d{2}:\d{2})/);
      return m ? m[1] : '';
    }

    /* Whole days between the date parts of two engine timestamps (0 when unknown) */
    function dayOffsetBetween(fromIso, toIso) {
      const a = Date.parse(`${String(fromIso || '').slice(0, 10)}T00:00:00Z`);
      const b = Date.parse(`${String(toIso || '').slice(0, 10)}T00:00:00Z`);
      return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, Math.round((b - a) / 86400000)) : 0;
    }

    /* Converts the side routes (flight + train/bus/transfer legs) of a /api/trip-search result
       into the card shape renderMultiModalSection expects. Map points are generated from leg coordinates. */
    function tripResultToMultiModal(tripRes, maxJourneyHours) {
      const options = Array.isArray(tripRes.options) ? tripRes.options : [];
      const cheapestDirect = Number(tripRes.cheapest_direct && tripRes.cheapest_direct.true_total_try) || 0;
      const maxMins = Number(maxJourneyHours) > 0 ? Number(maxJourneyHours) * 60 : Infinity;
      const query = tripRes.query || {};
      const routes = options
        .filter(o => o.kind === 'side_route' && (Number(o.duration_mins) || 0) <= maxMins)
        .slice(0, 10)
        .map((o, idx) => {
          const trueTotal = Number(o.true_total_try) || 0;
          const legs = Array.isArray(o.legs) ? o.legs : [];

          let mapPoints = null;
          if (legs.length > 0) {
            const firstLeg = legs[0];
            const lastLeg = legs[legs.length - 1];
            const origCoord = firstLeg && firstLeg.from_coord;
            const destCoord = lastLeg && lastLeg.to_coord;

            const flightLeg = legs.find(l => l.type === 'flight');
            let catchmCoord = null;
            let catchmName = '';
            let catchmCode = '';

            if (flightLeg && flightLeg.to_coord) {
              catchmCoord = flightLeg.to_coord;
              catchmName = flightLeg.to || '';
              catchmCode = flightLeg.to || '';
            } else if (legs.length >= 2) {
              const midLeg = legs[0];
              catchmCoord = midLeg.to_coord || (legs[1] && legs[1].from_coord);
              catchmName = midLeg.to || '';
              catchmCode = midLeg.to || '';
            }

            if (origCoord && origCoord.lat != null && origCoord.lon != null &&
                catchmCoord && catchmCoord.lat != null && catchmCoord.lon != null &&
                destCoord && destCoord.lat != null && destCoord.lon != null) {
              mapPoints = {
                origin: {
                  lat: Number(origCoord.lat),
                  lon: Number(origCoord.lon),
                  name: query.origin || (firstLeg && firstLeg.from) || ''
                },
                catchment: {
                  lat: Number(catchmCoord.lat),
                  lon: Number(catchmCoord.lon),
                  name: catchmName,
                  code: catchmCode
                },
                destination: {
                  lat: Number(destCoord.lat),
                  lon: Number(destCoord.lon),
                  name: query.destination || (lastLeg && lastLeg.to) || ''
                }
              };
            }
          }

          return {
            route_id: `R${idx + 1}`,
            origin_city: query.origin || '',
            destination_city: query.destination || '',
            total_price_try: Number(o.total_try) || 0,
            true_total_try: trueTotal,
            total_duration_mins: Number(o.duration_mins) || 0,
            arrival_day_offset: dayOffsetBetween(o.depart_at, o.arrive_at),
            initial_departure_time: hhmmOf(o.depart_at),
            final_arrival_time: hhmmOf(o.arrive_at),
            night_wait: !!o.night_wait,
            badges: Array.isArray(o.badges) ? o.badges.map(String) : [],
            savings_try: cheapestDirect > 0 ? Math.max(0, cheapestDirect - trueTotal) : 0,
            has_estimated_segments: !!o.has_estimated_segments,
            map_points: mapPoints,
            segments: legs.map(leg => ({
              type: leg.type,
              operator: leg.operator || '',
              from: leg.origin_station_name || leg.from || '',
              to: leg.dest_station_name || leg.to || '',
              departure_time: hhmmOf(leg.depart_at),
              arrival_time: hhmmOf(leg.arrive_at),
              price_try: Number(leg.price_try) || 0,
              is_estimated: !!leg.is_estimated,
              is_night_wait: false,
              booking_url: leg.booking_url || '',
              source: leg.source || null,
            })),
            nights_line: formatNightsLine(o),
          };
        });
      return { has_multimodal: routes.length > 0, multimodal_routes: routes };
    }

    const KNOWN_AIRPORT_CITIES = {
      'ATH': 'Atina',
      'SPATA': 'Atina',
      'SPATA-ARTEMIDA': 'Atina',
      'IST': 'İstanbul',
      'SAW': 'İstanbul',
      'VIE': 'Viyana',
      'PRG': 'Prag',
      'BUD': 'Budapeşte',
      'SOF': 'Sofya',
      'BEG': 'Belgrad',
      'SKP': 'Üsküp',
      'TGD': 'Podgorica',
      'TIA': 'Tiran',
      'SJJ': 'Saraybosna',
      'WAW': 'Varşova',
      'KRK': 'Krakov',
      'BTS': 'Bratislava',
      'KSC': 'Kösice',
      'AXD': 'Dedeağaç',
      'SKG': 'Selanik'
    };

    function canonicalCityName(place) {
      if (!place) return '';
      const p = String(place).trim();
      const up = p.toUpperCase();
      if (KNOWN_AIRPORT_CITIES[up]) return KNOWN_AIRPORT_CITIES[up];
      const low = p.toLowerCase();
      if (KNOWN_AIRPORT_CITIES[low.toUpperCase()]) return KNOWN_AIRPORT_CITIES[low.toUpperCase()];
      return p;
    }

    function formatNightsLine(opt) {
      if (!opt || typeof opt !== 'object') return '';
      if (typeof opt.nights_line === 'string' && opt.nights_line.trim()) {
        return opt.nights_line;
      }
      if (Array.isArray(opt.nights) && opt.nights.length > 0) {
        const parts = [];
        for (const n of opt.nights) {
          if (!n) continue;
          if (n.type === 'transit') {
            if (n.mode === 'flight') parts.push('✈️ yolda');
            else if (n.mode === 'train') parts.push('🚆 yolda');
            else parts.push('🚌 yolda');
          } else if (n.type === 'stay') {
            parts.push('🛏 ' + (n.place || ''));
          }
        }
        return parts.length ? ('Geceler: ' + parts.join(' · ')) : '';
      }

      const rawLegs = Array.isArray(opt.legs) && opt.legs.length ? opt.legs : (Array.isArray(opt.segments) ? opt.segments : []);
      if (!rawLegs.length) return '';

      const parsed = [];
      for (const leg of rawLegs) {
        if (!leg || typeof leg !== 'object') continue;
        const depStr = leg.depart_at || leg.depart_utc || leg.departure_time;
        const arrStr = leg.arrive_at || leg.arrive_utc || leg.arrival_time;
        if (!depStr || !arrStr) continue;
        const dep = new Date(depStr.length === 16 ? depStr + ':00' : depStr);
        const arr = new Date(arrStr.length === 16 ? arrStr + ':00' : arrStr);
        if (isNaN(dep.getTime()) || isNaN(arr.getTime())) continue;
        parsed.push({
          frm: String(leg.from || leg.origin_station || ''),
          to: String(leg.to || leg.dest_station || ''),
          dep,
          arr,
          raw: leg
        });
      }
      if (!parsed.length) return '';

      parsed.sort((a, b) => a.dep - b.dep);
      const firstDep = parsed[0].dep;
      const lastArr = parsed.reduce((max, l) => l.arr > max ? l.arr : max, parsed[0].arr);

      const startDay = new Date(firstDep.getFullYear(), firstDep.getMonth(), firstDep.getDate());
      const endDay = new Date(lastArr.getFullYear(), lastArr.getMonth(), lastArr.getDate());

      const parts = [];
      const cur = new Date(startDay);
      while (cur <= endDay) {
        const target = new Date(cur.getFullYear(), cur.getMonth(), cur.getDate(), 3, 0, 0);
        if (target > firstDep && target < lastArr) {
          let activeLeg = null;
          for (const l of parsed) {
            if (l.dep <= target && target <= l.arr) {
              activeLeg = l;
              break;
            }
          }
          if (activeLeg) {
            const rawType = String(activeLeg.raw.type || '').toLowerCase();
            if (rawType === 'flight') parts.push('✈️ yolda');
            else if (rawType === 'train' || rawType === 'rail') parts.push('🚆 yolda');
            else parts.push('🚌 yolda');
          } else {
            let prevLeg = null;
            for (const l of parsed) {
              if (l.arr <= target) prevLeg = l;
            }
            let isAirport = false;
            let placeRaw = '';
            if (prevLeg) {
              const toCode = String(prevLeg.to || '').trim().toUpperCase();
              const destStation = String(prevLeg.raw.dest_station_name || '');
              const isFlight = prevLeg.raw.type === 'flight';
              const isTransferToAirport = prevLeg.raw.type === 'transfer' && toCode.length === 3 && /^[A-Z]+$/.test(toCode);
              if (toCode.length === 3 && /^[A-Z]+$/.test(toCode) && (isFlight || isTransferToAirport || destStation.toLowerCase().includes('havaliman') || destStation.toLowerCase().includes('airport'))) {
                isAirport = true;
                placeRaw = toCode;
              } else if (destStation.toLowerCase().includes('havaliman') || destStation.toLowerCase().includes('airport')) {
                isAirport = true;
                placeRaw = destStation;
              } else {
                placeRaw = prevLeg.raw.dest_station_name || prevLeg.to;
              }
            } else {
              placeRaw = parsed[0].raw.origin_station_name || parsed[0].frm;
            }
            const canonical = canonicalCityName(placeRaw);
            const displayPlace = isAirport ? `${canonical} (havalimanı)` : canonical;
            parts.push(`🛏 ${displayPlace}`);
          }
        }
        cur.setDate(cur.getDate() + 1);
      }

      if (!parts.length) return '';
      return 'Geceler: ' + parts.join(' · ');
    }
    if (typeof window !== 'undefined') window.formatNightsLine = formatNightsLine;

    function renderMultiModalSection(mmData, searchDate) {
      const sec = document.getElementById('multimodalSection');
      const list = document.getElementById('multimodalCardsList');
      const pill = document.getElementById('multimodalSavingsSummary');
      if (!sec || !list) return;

      if (!mmData || !mmData.has_multimodal || !mmData.multimodal_routes || !mmData.multimodal_routes.length) {
        sec.style.display = 'none';
        return;
      }

      const routes = mmData.multimodal_routes;
      const maxSav = Math.max(...routes.map(routeSavingsTry));

      sec.style.display = 'block';
      if (maxSav > 0 && pill) {
        pill.style.display = 'inline-block';
        pill.innerHTML = `💡 Çevre havalimanı ve trenle <strong>${Math.round(maxSav).toLocaleString('tr-TR')} ₺</strong> tasarruf imkanı!`;
      } else if (pill) {
        pill.style.display = 'none';
      }

      list.innerHTML = routes.map(r => {
        const durHours = Math.floor(r.total_duration_mins / 60);
        const durMins = r.total_duration_mins % 60;
        const durStr = `${durHours} sa. ${durMins > 0 ? durMins + ' dk.' : ''}`;

        // Next-day arrival, night wait, hotel estimate
        const arrivalOffset = Number(r.arrival_day_offset) > 0 ? Math.trunc(Number(r.arrival_day_offset)) : 0;
        const arrivalDateStr = formatTrShortDate(searchDate, arrivalOffset);
        const nextDayPill = arrivalOffset > 0 ? `<span class="mm-pill-nextday">+${arrivalOffset} gün</span>` : '';
        const nightWaitBadge = r.night_wait
          ? `<span class="mm-pill-night">🌙 Gece beklemesi${hasNumber(r.night_wait_mins) ? ': ' + formatSaDk(r.night_wait_mins) : ''}</span>`
          : '';
        const hotelNum = hasNumber(r.estimated_hotel_try) ? r.estimated_hotel_try : null;
        const trueTotal = hasNumber(r.true_total_try) ? r.true_total_try : r.total_price_try;
        const hotelHtml = hotelNum !== null
          ? `<div style="font-size: 12px; color: var(--text-muted); font-weight: 700;">+ ~${Math.round(hotelNum).toLocaleString('tr-TR')} ₺ otel (tahmini)</div>
             <div style="font-size: 12px; color: #b45309; font-weight: 800;">Gerçek toplam: ~${Math.round(trueTotal).toLocaleString('tr-TR')} ₺</div>`
          : '';
        const savingsVal = routeSavingsTry(r);

        const badgesHtml = (r.badges || []).map(b => {
          let color = '#0284c7';
          let bg = 'rgba(2, 132, 199, 0.15)';
          if (b.includes('Ucuz')) { color = '#10b981'; bg = 'rgba(16, 185, 129, 0.15)'; }
          else if (b.includes('Hızlı')) { color = '#d97706'; bg = 'rgba(217, 119, 6, 0.15)'; }
          else if (b.includes('Değer')) { color = '#7c3aed'; bg = 'rgba(124, 58, 237, 0.15)'; }
          return `<span style="font-size: 11px; font-weight: 800; color: ${color}; background: ${bg}; padding: 3px 8px; border-radius: 6px;">🏷️ ${escapeHtml(b)}</span>`;
        }).join(' ');

        const savingsHtml = savingsVal > 0
          ? `<span style="font-size: 13px; font-weight: 800; color: #10b981; background: rgba(16, 185, 129, 0.15); padding: 4px 10px; border-radius: 6px;">💰 ${Math.round(savingsVal).toLocaleString('tr-TR')} ₺ Tasarruf</span>`
          : '';

        const timelineHtml = (r.segments || []).map(seg => {
          const typeClass = seg.type === 'flight' ? 'flight' : (seg.type === 'transfer' ? 'transfer' : 'train');
          let icon = '✈️';
          let title = 'Uçuş';
          if (seg.type === 'transfer') { icon = '🔄'; title = 'Aktarma & İstasyon'; }
          else if (seg.type === 'train') { icon = '🚆'; title = 'Hızlı Tren'; }
          else if (seg.type === 'bus') { icon = '🚌'; title = 'Otobüs'; }
          const isNightWait = !!seg.is_night_wait;
          if (isNightWait) { icon = '🌙'; title = 'Gece Beklemesi'; }

          const priceText = seg.price_try > 0 ? ` · <strong style="color: var(--accent);">${Math.round(seg.price_try).toLocaleString('tr-TR')} ₺</strong>` : '';
          const estimatedBadge = seg.is_estimated
            ? ` <span style="font-size: 10px; font-weight: 800; color: #b45309; background: rgba(245, 158, 11, 0.15); padding: 2px 6px; border-radius: 4px; margin-left: 4px;">⚠️ Tahmini tarife</span>`
            : '';
          const sourceTag = (seg.type === 'train' || seg.type === 'bus') ? groundSourceTag(seg) : '';
          const noteText = seg.note ?`<div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">ℹ️ ${escapeHtml(seg.note)}</div>` : '';
          const nightNote = isNightWait ? `<div style="font-size: 11px; font-weight: 700; color: #b45309; margin-top: 2px;">🌙 Gece beklemesi — otel gerekebilir</div>` : '';
          const bookingBtn = seg.booking_url ? `
            <a href="${safeUrl(seg.booking_url)}" target="_blank" rel="noopener noreferrer"
               style="font-size: 11px; font-weight: 700; color: #0284c7; background: rgba(2, 132, 199, 0.12); padding: 3px 8px; border-radius: 6px; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; margin-left: 8px;">
               🎫 Bilet Al ↗
            </a>
          ` : '';

          return `
            <div class="timeline-node ${typeClass}${isNightWait ? ' night-wait' : ''}">
              <div style="font-weight: 700; color: var(--text); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px;">
                <div>
                  <span>${icon} <strong>${escapeHtml(title)}:</strong> ${escapeHtml(seg.operator)} (${escapeHtml(seg.from)} ➔ ${escapeHtml(seg.to)})</span>
                  <span style="font-size: 12px; color: var(--text-muted); margin-left: 8px;">⏰ ${escapeHtml(seg.departure_time)} – ${escapeHtml(seg.arrival_time)}</span>
                  ${priceText}${estimatedBadge}${sourceTag}
                </div>
                ${bookingBtn}
              </div>
              ${nightNote}${noteText}
            </div>
          `;
        }).join('');

        return `
          <div class="multimodal-card" onmouseenter="highlightMapRoute(${jsArg(r.route_id)})" style="cursor: pointer;">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 10px; border-bottom: 1px solid var(--card-border); padding-bottom: 12px;">
              <div>
                <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-bottom: 6px;">
                  <span style="font-size: 12px; font-weight: 800; color: var(--accent); background: rgba(56, 189, 248, 0.12); padding: 2px 8px; border-radius: 6px;">${escapeHtml(r.route_id)}</span>
                  ${badgesHtml}
                  ${savingsHtml}
                  ${nightWaitBadge}
                </div>
                <div style="font-size: 15px; font-weight: 800; color: var(--text);">
                  ${escapeHtml(r.origin_city)} ➔ ${escapeHtml(r.destination_city)}
                  <span style="font-size: 13px; font-weight: 600; color: var(--text-muted); margin-left: 8px;">(İlk Kalkış: ${escapeHtml(r.initial_departure_time)} · Nihai Varış: ${escapeHtml(r.final_arrival_time)}${nextDayPill})</span>
                </div>
                ${arrivalDateStr ? `<div style="font-size: 12px; color: var(--text-muted); font-weight: 600; margin-top: 3px;">📅 Varış: ${escapeHtml(arrivalDateStr)}</div>` : ''}
                ${r.nights_line || formatNightsLine(r) ? `<div class="tp-nights" style="font-size: 13px; font-weight: 600; color: var(--text-muted); margin-top: 4px;">${escapeHtml(r.nights_line || formatNightsLine(r))}</div>` : ''}
              </div>
              <div style="text-align: right;">
                <div style="font-size: 22px; font-weight: 800; color: var(--text);">${Math.round(r.total_price_try).toLocaleString('tr-TR')} ₺</div>
                ${hotelHtml}
                <div style="font-size: 12px; color: var(--text-muted); font-weight: 700;">⏱️ Toplam: ${durStr}</div>
              </div>
            </div>

            <div class="timeline-container">
              ${timelineHtml}
            </div>

            ${r.has_estimated_segments ? `<div style="font-size: 11px; font-weight: 600; color: #b45309; background: rgba(245, 158, 11, 0.1); padding: 6px 10px; border-radius: 6px; margin-top: 8px;">Tren/otobüs saat ve fiyatları tahminidir; bilet almadan önce operatör sitesinden kontrol edin.</div>` : ''}

            <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px dashed var(--card-border); padding-top: 10px; margin-top: 10px; flex-wrap: wrap; gap: 8px;">
              <span style="font-size: 11px; color: var(--text-muted);">🛡️ Uçuş ve tren biletleri resmi sağlayıcılardan bağımsız temin edilir. Asgari transfer tamponu dahildir.</span>
              <div style="display: flex; gap: 8px;">
                ${r.map_points ? `<button class="filter-btn" style="font-size: 12px; padding: 6px 12px;" onclick="focusMapOnRoute(${jsArg(r.route_id)})">🗺️ Haritada Odakla</button>` : ''}
                <button class="filter-btn active" style="font-size: 12px; padding: 6px 14px;" onclick="openAlarmModalWithPrice(${r.total_price_try}, true)">🔔 Hibrit Fiyat Alarmı</button>
              </div>
            </div>
          </div>
        `;
      }).join('');

      renderMultiModalMap(routes);
    }

    function renderMultiModalMap(routes) {
      if (typeof L === 'undefined') return;
      const container = document.getElementById('multimodalMap');
      if (!container) return;

      if (window._mmMap) {
        window._mmMap.remove();
        window._mmMap = null;
      }
      if (container._leaflet_id) {
        container._leaflet_id = null;
      }

      const allCoords = [];
      routes.forEach(r => {
        if (r.map_points) {
          if (r.map_points.origin && r.map_points.origin.lat) allCoords.push([r.map_points.origin.lat, r.map_points.origin.lon]);
          if (r.map_points.catchment && r.map_points.catchment.lat) allCoords.push([r.map_points.catchment.lat, r.map_points.catchment.lon]);
          if (r.map_points.destination && r.map_points.destination.lat) allCoords.push([r.map_points.destination.lat, r.map_points.destination.lon]);
        }
      });

      // Routes without map points (trip-search results) hide the map box instead of showing an empty map
      const mapBox = document.getElementById('multimodalMapContainer');
      if (mapBox) mapBox.style.display = allCoords.length ? '' : 'none';
      if (!allCoords.length) return;

      const map = L.map('multimodalMap', {
        scrollWheelZoom: false,
        zoomControl: true
      });
      window._mmMap = map;

      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '© OpenStreetMap'
      }).addTo(map);

      window._routeLayers = {};

      routes.forEach((r, idx) => {
        if (!r.map_points) return;
        const orig = r.map_points.origin;
        const catchm = r.map_points.catchment;
        const dest = r.map_points.destination;
        if (!orig || !catchm || !dest) return;

        const layerGroup = L.layerGroup();
        const isPrimary = (idx === 0);

        // Flight line (dashed blue arc)
        const flightLine = L.polyline([
          [orig.lat, orig.lon],
          [catchm.lat, catchm.lon]
        ], {
          color: '#0284c7',
          weight: isPrimary ? 4 : 2,
          opacity: isPrimary ? 0.95 : 0.4,
          dashArray: '8, 6'
        }).bindPopup(`<strong>✈️ Uçuş:</strong> ${escapeHtml(r.origin_city)} ➔ ${escapeHtml(catchm.name)} (${escapeHtml(catchm.code)})<br>Fiyat: ${Math.round(r.segments[0]?.price_try || 0).toLocaleString('tr-TR')} ₺`);

        // Train line (solid emerald)
        const trainLine = L.polyline([
          [catchm.lat, catchm.lon],
          [dest.lat, dest.lon]
        ], {
          color: '#10b981',
          weight: isPrimary ? 5 : 2,
          opacity: isPrimary ? 1.0 : 0.4
        }).bindPopup(`<strong>🚆 Tren:</strong> ${escapeHtml(catchm.name)} ➔ ${escapeHtml(r.destination_city)}<br>Operatör: ${escapeHtml(r.segments[2]?.operator || 'Tren')}`);

        // Point Markers
        const origMarker = L.circleMarker([orig.lat, orig.lon], {
          radius: 6,
          fillColor: '#0284c7',
          color: '#ffffff',
          weight: 2,
          fillOpacity: 1
        }).bindPopup(`<strong>🛫 Başlangıç:</strong> ${escapeHtml(r.origin_city)}`);

        const catchMarker = L.circleMarker([catchm.lat, catchm.lon], {
          radius: 7,
          fillColor: '#f59e0b',
          color: '#ffffff',
          weight: 2,
          fillOpacity: 1
        }).bindPopup(`<strong>🔄 Aktarma Havalimanı:</strong> ${escapeHtml(catchm.name)} (${escapeHtml(catchm.code)})`);

        const destMarker = L.circleMarker([dest.lat, dest.lon], {
          radius: 8,
          fillColor: '#10b981',
          color: '#ffffff',
          weight: 2,
          fillOpacity: 1
        }).bindPopup(`<strong>🏁 Hedef:</strong> ${escapeHtml(r.destination_city)}`);

        layerGroup.addLayer(flightLine);
        layerGroup.addLayer(trainLine);
        layerGroup.addLayer(origMarker);
        layerGroup.addLayer(catchMarker);
        layerGroup.addLayer(destMarker);

        layerGroup.addTo(map);
        window._routeLayers[r.route_id] = { layerGroup, flightLine, trainLine, r };
      });

      const bounds = L.latLngBounds(allCoords);
      map.fitBounds(bounds, { padding: [35, 35] });

      setTimeout(() => { map.invalidateSize(); }, 250);
    }

    function highlightMapRoute(routeId) {
      if (!window._routeLayers) return;
      Object.keys(window._routeLayers).forEach(id => {
        const entry = window._routeLayers[id];
        if (id === routeId) {
          entry.flightLine.setStyle({ weight: 5, opacity: 1 });
          entry.trainLine.setStyle({ weight: 6, opacity: 1 });
          entry.layerGroup.bringToFront();
        } else {
          entry.flightLine.setStyle({ weight: 2, opacity: 0.25 });
          entry.trainLine.setStyle({ weight: 2, opacity: 0.25 });
        }
      });
    }

    function focusMapOnRoute(routeId) {
      if (typeof L === 'undefined' || !window._routeLayers || !window._mmMap) return;
      const entry = window._routeLayers[routeId];
      if (!entry || !entry.r || !entry.r.map_points) return;
      highlightMapRoute(routeId);
      const mp = entry.r.map_points;
      const pts = [
        [mp.origin.lat, mp.origin.lon],
        [mp.catchment.lat, mp.catchment.lon],
        [mp.destination.lat, mp.destination.lon]
      ];
      window._mmMap.fitBounds(L.latLngBounds(pts), { padding: [40, 40] });
      const mapEl = document.getElementById('multimodalMapContainer');
      if (mapEl) mapEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    async function searchLiveFlights() {
      const origin = document.getElementById('origin').value.trim();
      const destination = document.getElementById('destination').value.trim();
      const depDate = document.getElementById('depDate').value;
      const retDate = document.getElementById('retDate').value;
      const listEl = document.getElementById('liveFlightsList');
      const btnSearch = document.querySelector('.btn-search');

      if (!origin || !destination) {
        alert('Lütfen kalkış ve varış şehirlerini girin.');
        return;
      }

      hideResultsUI();
      if (btnSearch) {
        btnSearch.disabled = true;
        btnSearch.style.opacity = '0.6';
      }

      listEl.innerHTML = `
        <div style="text-align: center; padding: 50px 20px; background: var(--surface-bg); border-radius: 14px; border: 1px solid var(--card-border); color: var(--accent);">
          <div style="font-size: 32px; margin-bottom: 12px;">⚡</div>
          <div style="font-size: 16px; font-weight: 700; color: var(--text);">${escapeHtml(origin)} ➔ ${escapeHtml(destination)} canlı taranıyor...</div>
          <div style="font-size: 13px; color: var(--text-muted); margin-top: 6px;">
            Google Flights & Çevre Havalimanları taranıyor, lütfen birkaç saniye bekleyin (~6-10 sn)...
          </div>
        </div>
      `;

      let queryUrl = `/api/live-flights?origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}&departure_date=${depDate}&trip_type=${currentTripType}`;
      if (currentTripType === 'roundtrip' && retDate) {
        queryUrl += `&return_date=${retDate}`;
      }

      // Check Multi-Modal request: side routes come from the route engine (/api/trip-search)
      const chkMM = document.getElementById('chkMultiModal');
      let mmPromise = null;
      if (chkMM && chkMM.checked) {
        const deadline = document.getElementById('inputArrivalDeadline').value.trim();
        const maxJourney = document.getElementById('selectMaxJourneyHours').value.trim();
        let mmUrl = `/api/trip-search?origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}&depart_dates=${depDate}&include_side_routes=1`;
        if (deadline) mmUrl += `&arrive_by=${encodeURIComponent(`${depDate}T${deadline}`)}`;
        mmUrl += `&time_value_try_per_hour=${encodeURIComponent(readTimeValueSetting())}`;
        mmPromise = fetch(mmUrl)
          .then(r => r.ok ? r.json() : null)
          .then(tripRes => tripRes ? tripResultToMultiModal(tripRes, maxJourney) : null)
          .catch(err => {
            console.warn('Multimodal fetch error:', err);
            return null;
          });
      }

      try {
        const res = await fetch(queryUrl);
        if (!res.ok) {
          const msg = await readApiErrorMessage(res, 'Canlı uçuş araması sırasında hata oluştu veya bağlantı koptu. Lütfen tekrar deneyin.');
          listEl.innerHTML = `<div style="color: #ef4444; text-align: center; padding: 30px; background: var(--surface-bg); border-radius: 14px; border: 1px solid var(--card-border);">${escapeHtml(msg)}</div>`;
          return;
        }
        const data = await res.json();

        allFlights = data.flights || [];

        // Save session atomically
        if (allFlights.length > 0) {
          const session = {
            origin,
            destination,
            depDate,
            retDate,
            tripType: currentTripType,
            flights: allFlights,
            dateMatrix: data.date_matrix,
            priceHistory: data.price_history
          };
          localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
        }

        renderFlightDashboard(allFlights, origin, destination, currentTripType, data.date_matrix, data.price_history);

        if (mmPromise) {
          const mmData = await mmPromise;
          renderMultiModalSection(mmData, depDate);
        } else {
          const mmEl = document.getElementById('multimodalSection');
          if (mmEl) mmEl.style.display = 'none';
        }
      } catch (e) {
        console.error('Search error:', e);
        listEl.innerHTML = '<div style="color: #ef4444; text-align: center; padding: 30px; background: var(--surface-bg); border-radius: 14px; border: 1px solid var(--card-border);">Canlı uçuş araması sırasında hata oluştu veya bağlantı koptu. Lütfen tekrar deneyin.</div>';
      } finally {
        if (btnSearch) {
          btnSearch.disabled = false;
          btnSearch.style.opacity = '1';
        }
      }
    }

    /* Parse minutes from duration string e.g. "17 sa. 20 dk." */
    function parseDurationMinutes(durationStr) {
      if (!durationStr) return 9999;
      let total = 0;
      const hMatch = durationStr.match(/(\d+)\s*sa/);
      const mMatch = durationStr.match(/(\d+)\s*dk/);
      if (hMatch) total += parseInt(hMatch[1]) * 60;
      if (mMatch) total += parseInt(mMatch[1]);
      return total || 9999;
    }

    let priceChartInstance = null;

    function renderDateMatrix(matrixData) {
      const container = document.getElementById('dateMatrixContainer');
      if (!matrixData || !matrixData.days || !matrixData.days.length) {
        if (container) container.style.display = 'none';
        return;
      }
      container.style.display = 'block';

      const savingPill = document.getElementById('matrixSavingPill');
      if (matrixData.max_saving_try > 0 && matrixData.best_display) {
        savingPill.style.display = 'inline-block';
        savingPill.innerHTML = `💡 <strong>${escapeHtml(matrixData.best_display)}</strong> günü uçarsanız <strong>${matrixData.max_saving_try.toLocaleString('tr-TR')} ₺</strong> daha uygun!`;
      } else {
        savingPill.style.display = 'none';
      }

      const grid = document.getElementById('dateMatrixGrid');
      grid.innerHTML = matrixData.days.map(d => {
        let cardClass = 'matrix-day-card';
        let badgeHtml = '';
        let priceHtml = '';
        
        const hasValidPrice = (d.has_price !== false) && (d.price !== null) && (d.price !== undefined) && (d.price > 0);

        if (d.is_selected) {
          cardClass += ' selected';
          badgeHtml = `<span class="matrix-badge" style="background: rgba(56, 189, 248, 0.2); color: #38bdf8;">📍 Seçili Tarih</span>`;
        } else if (d.is_cheapest && hasValidPrice) {
          cardClass += ' cheapest';
          badgeHtml = `<span class="matrix-badge" style="background: rgba(16, 185, 129, 0.2); color: #10b981; font-weight: 800;">🟢 En Uygun Gün</span>`;
        } else if (hasValidPrice) {
          badgeHtml = `<span class="matrix-badge" style="background: rgba(56, 189, 248, 0.12); color: var(--accent); font-weight: 700;">Google Flights</span>`;
        } else {
          badgeHtml = `<span class="matrix-badge" style="background: rgba(148, 163, 184, 0.12); color: var(--text-muted); opacity: 0.7;">Uçuş Yok</span>`;
        }

        if (hasValidPrice) {
          const priceFormatted = Math.round(d.price).toLocaleString('tr-TR');
          priceHtml = `${priceFormatted} ₺`;
        } else {
          priceHtml = `-- ₺`;
        }

        return `
          <div class="${cardClass}" onclick="selectMatrixDate(${jsArg(d.date)})" title="${escapeHtml(d.display_label)} için canlı uçuşları ara">
            <div class="matrix-date-title">${escapeHtml(d.display_label)}</div>
            <div class="matrix-price">${priceHtml}</div>
            ${d.airline && hasValidPrice && d.airline !== 'Google Flights' ? `<div style="font-size: 10px; color: var(--text-muted); margin-bottom: 4px; font-weight: 600;">✈️ ${escapeHtml(d.airline)}</div>` : ''}
            <div>${badgeHtml}</div>
          </div>
        `;
      }).join('');
    }

    function selectMatrixDate(dateStr) {
      document.getElementById('depDate').value = dateStr;
      searchLiveFlights();
    }

    let lastHistoryPoints = null;
    let currentHistoryDays = 1;

    async function loadPriceHistoryDays(days) {
      currentHistoryDays = days;
      const b1 = document.getElementById('btnHist1');
      const b7 = document.getElementById('btnHist7');
      const b14 = document.getElementById('btnHist14');
      const b30 = document.getElementById('btnHist30');
      if (b1) b1.classList.toggle('active', days === 1);
      if (b7) b7.classList.toggle('active', days === 7);
      if (b14) b14.classList.toggle('active', days === 14);
      if (b30) b30.classList.toggle('active', days === 30);

      const origin = document.getElementById('origin').value.trim();
      const destination = document.getElementById('destination').value.trim();
      const depDate = document.getElementById('depDate').value;
      const cheapestPrice = allFlights.length > 0 ? allFlights[0].total_price_try : 0;
      const cheapestAirline = allFlights.length > 0 ? allFlights[0].airline : '';

      try {
        const resp = await fetch(`/api/price-history?origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}&departure_date=${depDate}&trip_type=${currentTripType}&current_price=${cheapestPrice}&airline=${encodeURIComponent(cheapestAirline)}&days=${days}`);
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.history) {
            renderPriceHistoryChart(data.history);
          }
        }
      } catch (e) {
        console.error('Error fetching price history:', e);
      }
    }

    function renderPriceHistoryChart(historyPoints) {
      lastHistoryPoints = historyPoints;
      const container = document.getElementById('priceHistoryContainer');
      if (!historyPoints || !historyPoints.length) {
        if (container) container.style.display = 'none';
        return;
      }
      container.style.display = 'block';

      const canvas = document.getElementById('priceHistoryChart');
      if (!canvas) return;
      const ctx = canvas.getContext('2d');

      const labels = historyPoints.map(p => p.time_label || p.date_label || '');
      const data = historyPoints.map(p => p.price);
      const minP = Math.min(...data);
      const maxP = Math.max(...data);

      const minLabel = document.getElementById('chartMinPriceLabel');
      if (minLabel) {
        minLabel.innerText = `Dip Fiyat: ${minP.toLocaleString('tr-TR')} ₺`;
      }

      if (priceChartInstance) {
        priceChartInstance.destroy();
      }

      const isDark = document.body.classList.contains('dark-theme');
      const lineColor = isDark ? '#10b981' : '#0284c7';
      const pointColor = isDark ? '#10b981' : '#0284c7';
      const softPointColor = isDark ? 'rgba(16, 185, 129, 0.55)' : 'rgba(2, 132, 199, 0.55)';
      const gridColor = isDark ? 'rgba(255, 255, 255, 0.05)' : 'rgba(0, 0, 0, 0.06)';
      const tickColor = isDark ? '#94a3b8' : '#64748b';

      // Estimated (model) points: hollow, faded markers; segments touching them are dashed.
      const isEstAt = (i) => !!(historyPoints[i] && historyPoints[i].is_estimated);
      const hasEstimated = historyPoints.some(p => p && p.is_estimated);
      const estNote = document.getElementById('priceHistoryEstimateNote');
      if (estNote) estNote.style.display = hasEstimated ? 'block' : 'none';
      const pointBgColors = historyPoints.map((p, i) => isEstAt(i) ? 'rgba(0, 0, 0, 0)' : pointColor);
      const pointBorderColors = historyPoints.map((p, i) => isEstAt(i) ? softPointColor : '#ffffff');
      const pointRadii = historyPoints.map((p, i) => isEstAt(i) ? 3.5 : 4);

      // Smooth gradient fill
      const gradient = ctx.createLinearGradient(0, 0, 0, 160);
      if (isDark) {
        gradient.addColorStop(0, 'rgba(16, 185, 129, 0.35)');
        gradient.addColorStop(1, 'rgba(16, 185, 129, 0.01)');
      } else {
        gradient.addColorStop(0, 'rgba(2, 132, 199, 0.22)');
        gradient.addColorStop(1, 'rgba(2, 132, 199, 0.01)');
      }

      priceChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: 'En Düşük Bilet (₺)',
            data: data,
            borderColor: lineColor,
            backgroundColor: gradient,
            borderWidth: 2.5,
            fill: true,
            tension: 0.35,
            segment: {
              borderDash: (ctx) => (isEstAt(ctx.p0DataIndex) || isEstAt(ctx.p1DataIndex)) ? [6, 4] : undefined,
              borderColor: (ctx) => (isEstAt(ctx.p0DataIndex) || isEstAt(ctx.p1DataIndex)) ? softPointColor : undefined,
            },
            pointBackgroundColor: pointBgColors,
            pointBorderColor: pointBorderColors,
            pointBorderWidth: 1.5,
            pointRadius: pointRadii,
            pointHoverRadius: 7,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            intersect: false,
            mode: 'index',
          },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: isDark ? '#0f172a' : '#0f172a',
              titleColor: '#f8fafc',
              bodyColor: '#38bdf8',
              borderColor: isDark ? '#1e293b' : '#cbd5e1',
              borderWidth: 1,
              padding: 10,
              displayColors: false,
              callbacks: {
                label: function(context) {
                  const pt = historyPoints[context.dataIndex];
                  const pVal = context.parsed.y.toLocaleString('tr-TR') + ' ₺';
                  const liveTag = (pt && pt.is_estimated) ? '📊 Tahmini (gözlem değil)'
                    : ((pt && pt.is_live) ? '🟢 Canlı Kayıt' : '📊 Sektör Tahmini');
                  const airlineStr = pt && pt.airline ? ` (${pt.airline})` : '';
                  return `${liveTag}: ${pVal}${airlineStr}`;
                }
              }
            }
          },
          scales: {
            x: {
              grid: { color: gridColor },
              ticks: { color: tickColor, font: { size: 11 } }
            },
            y: {
              grid: { color: gridColor },
              ticks: {
                color: tickColor,
                font: { size: 11 },
                callback: function(value) {
                  return value.toLocaleString('tr-TR') + ' ₺';
                }
              },
              suggestedMin: Math.max(0, minP * 0.95),
              suggestedMax: maxP * 1.05,
            }
          }
        }
      });
    }

    function toggleChartVisibility() {
      const wrapper = document.getElementById('priceHistoryChartWrapper');
      if (wrapper) {
        wrapper.style.display = wrapper.style.display === 'none' ? 'block' : 'none';
      }
    }

    function setTheme(theme) {
      const isDark = theme === 'dark';
      if (isDark) {
        document.body.classList.add('dark-theme');
        const iconEl = document.getElementById('themeIcon');
        if (iconEl) iconEl.innerText = '🌙';
        const labelEl = document.getElementById('themeLabel');
        if (labelEl) labelEl.innerText = 'Koyu';
      } else {
        document.body.classList.remove('dark-theme');
        const iconEl = document.getElementById('themeIcon');
        if (iconEl) iconEl.innerText = '☀️';
        const labelEl = document.getElementById('themeLabel');
        if (labelEl) labelEl.innerText = 'Açık';
      }
      localStorage.setItem('travelplan_theme', theme);
      if (lastHistoryPoints && lastHistoryPoints.length > 0) {
        renderPriceHistoryChart(lastHistoryPoints);
      }
    }

    function toggleTheme() {
      const isDark = document.body.classList.contains('dark-theme');
      setTheme(isDark ? 'light' : 'dark');
    }

    function renderFlightDashboard(flights, origin, destination, tripType, dateMatrix, priceHistory) {
      const listEl = document.getElementById('liveFlightsList');
      if (!flights || !flights.length) {
        hideResultsUI();
        listEl.innerHTML = `<div style="text-align: center; padding: 40px; color: var(--text-muted); background: var(--surface-bg); border-radius: 14px;">${escapeHtml(origin)} ➔ ${escapeHtml(destination)} rotası için bilet bulunamadı.</div>`;
        return;
      }

      // Calculate 3 Summary Highlights:
      // 1. Cheapest
      const cheapest = [...flights].sort((a, b) => a.total_price_try - b.total_price_try)[0];
      
      // 2. Fastest (prefer direct, with a 15-minute tolerance so we pick the best value among virtually identical speeds)
      const directFlights = flights.filter(f => f.is_direct || f.stops_count === 0);
      let fastest;
      if (directFlights.length > 0) {
        const sortedByDuration = [...directFlights].sort((a, b) => parseDurationMinutes(a.duration) - parseDurationMinutes(b.duration));
        const minDirectMins = parseDurationMinutes(sortedByDuration[0].duration);
        // Flights within 15 mins of the absolute fastest are practically identical in travel time
        // Among these, pick the most cost-effective one!
        const closeContenders = sortedByDuration.filter(f => parseDurationMinutes(f.duration) <= minDirectMins + 15);
        fastest = [...closeContenders].sort((a, b) => a.total_price_try - b.total_price_try)[0];
      } else {
        fastest = [...flights].sort((a, b) => parseDurationMinutes(a.duration) - parseDurationMinutes(b.duration))[0];
      }

      // 3. Smart Best Value (F/P Dengesi):
      const rankedFP = [...flights].sort((a, b) => {
        const scoreA = a.total_price_try + (parseDurationMinutes(a.duration) * 8.5);
        const scoreB = b.total_price_try + (parseDurationMinutes(b.duration) * 8.5);
        return scoreA - scoreB;
      });

      let best = rankedFP[0];
      // If best F/P is identical to cheapest, only pick an alternative if it saves substantial flight time (>= 60 mins)
      if (best === cheapest && rankedFP.length > 1) {
        const cheapMins = parseDurationMinutes(cheapest.duration);
        const fasterAlt = rankedFP.find(f => f !== cheapest && parseDurationMinutes(f.duration) <= cheapMins - 60);
        if (fasterAlt) {
          best = fasterAlt;
        }
        // If no faster alternative exists, cheapest is genuinely the Best Value!
      }

      // Populate 3'lü Özet Kartları
      document.getElementById('sumPriceCheapest').innerText = `${cheapest.total_price_try.toLocaleString('tr-TR')} ₺`;
      document.getElementById('sumAirlineCheapest').innerText = cheapest.airline;
      document.getElementById('sumMetaCheapest').innerText = `${cheapest.duration} · ${cheapest.stops}`;

      document.getElementById('sumPriceBest').innerText = `${best.total_price_try.toLocaleString('tr-TR')} ₺`;
      document.getElementById('sumAirlineBest').innerText = best.airline;
      document.getElementById('sumMetaBest').innerText = `${best.duration} · ${best.stops}`;

      document.getElementById('sumPriceFastest').innerText = `${fastest.total_price_try.toLocaleString('tr-TR')} ₺`;
      document.getElementById('sumAirlineFastest').innerText = fastest.airline;
      document.getElementById('sumMetaFastest').innerText = `${fastest.duration} · ${fastest.stops}`;

      document.getElementById('summaryCardsContainer').style.display = 'grid';

      // Render Date Matrix (±3 Gün)
      renderDateMatrix(dateMatrix);

      // Render Price History (Chart.js)
      renderPriceHistoryChart(priceHistory);

      // Setup Airline Chips in Filter Toolbar
      buildAirlineFilterChips(flights);

      // Show Filter Toolbar
      document.getElementById('filterToolbar').style.display = 'flex';
      document.getElementById('totalFlightCount').innerText = flights.length;

      // Render filtered list
      applyFiltersAndRender();
    }

    function buildAirlineFilterChips(flights) {
      const container = document.getElementById('airlineChipsContainer');
      const airlines = Array.from(new Set(flights.map(f => {
        return f.airline.split(',')[0].split('/')[0].trim();
      }))).slice(0, 5);

      container.innerHTML = `
        <button class="filter-btn ${!selectedAirline ? 'active' : ''}" onclick="selectAirlineFilter(null)">Tümü</button>
        ${airlines.map(a => `
          <button class="filter-btn ${selectedAirline === a ? 'active' : ''}" onclick="selectAirlineFilter(${jsArg(a)})">${escapeHtml(a)}</button>
        `).join('')}
      `;
    }

    function setSortMode(mode) {
      currentSort = mode;
      document.getElementById('btnSortPrice').classList.toggle('active', mode === 'price');
      document.getElementById('btnSortDuration').classList.toggle('active', mode === 'duration');
      applyFiltersAndRender();
    }

    function setStopsFilter(val) {
      selectedStops = val;
      document.getElementById('btnStopAll').classList.toggle('active', val === 'all');
      document.getElementById('btnStop0').classList.toggle('active', val === 0);
      document.getElementById('btnStop1').classList.toggle('active', val === 1);
      document.getElementById('btnStop2').classList.toggle('active', val === 2);
      applyFiltersAndRender();
    }

    let filterCustomStart = '00:00';
    let filterCustomEnd = '24:00';

    function parseTimeToMinutes(str, defaultMins = 0) {
      if (!str) return defaultMins;
      let clean = String(str).trim().replace(/[.\s;]+/, ':');
      if (clean.includes(':')) {
        const parts = clean.split(':');
        const h = parseInt(parts[0], 10);
        const m = parseInt(parts[1], 10);
        if (isNaN(h)) return defaultMins;
        if (h >= 24) return 24 * 60;
        return h * 60 + (isNaN(m) ? 0 : Math.min(m, 59));
      } else if (/^\d{3,4}$/.test(clean)) {
        const h = parseInt(clean.slice(0, clean.length - 2), 10);
        const m = parseInt(clean.slice(-2), 10);
        if (h >= 24) return 24 * 60;
        return h * 60 + Math.min(m, 59);
      } else {
        const h = parseInt(clean, 10);
        if (isNaN(h)) return defaultMins;
        if (h >= 24) return 24 * 60;
        return h * 60;
      }
    }

    function formatMinutesToTime(totalMins) {
      let h = Math.floor(totalMins / 60);
      let m = totalMins % 60;
      if (h >= 24) return '24:00';
      return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
    }

    function parseFlexibleTime(str, defaultHour = 0) {
      if (!str) return formatMinutesToTime(defaultHour * 60);
      let raw = String(str).trim();
      let clean = raw.replace(/[.\s;]+/, ':');
      if (/^\d{3,4}$/.test(clean)) {
        let h = parseInt(clean.slice(0, clean.length - 2), 10) || 0;
        let m = parseInt(clean.slice(-2), 10) || 0;
        if (h > 24) h = 24;
        if (h === 24) m = 0;
        if (m > 59) m = 59;
        return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
      }
      if (clean.includes(':')) {
        let [hStr, mStr] = clean.split(':');
        let h = parseInt(hStr, 10);
        if (isNaN(h)) h = defaultHour;
        let m = parseInt(mStr, 10);
        if (isNaN(m)) m = 0;
        if (h > 24) h = 24;
        if (h === 24) m = 0;
        if (m > 59) m = 59;
        return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
      }
      let h = parseInt(clean, 10);
      if (!isNaN(h)) {
        if (h > 24) h = 24;
        return String(h).padStart(2, '0') + ':00';
      }
      return formatMinutesToTime(defaultHour * 60);
    }

    function setDepartureTimeFilter(slot) {
      selectedTimeSlot = slot;
      document.getElementById('btnTimeAll').classList.toggle('active', slot === 'all');
      document.getElementById('btnTimeMorning').classList.toggle('active', slot === 'morning');
      document.getElementById('btnTimeAfternoon').classList.toggle('active', slot === 'afternoon');
      document.getElementById('btnTimeEvening').classList.toggle('active', slot === 'evening');
      const bNight = document.getElementById('btnTimeNight');
      if (bNight) bNight.classList.toggle('active', slot === 'night');
      const bCustom = document.getElementById('btnTimeCustom');
      if (bCustom) bCustom.classList.toggle('active', slot === 'custom');
      const customPanel = document.getElementById('mainCustomTimePanel');
      if (customPanel) customPanel.style.display = (slot === 'custom') ? 'flex' : 'none';
      applyFiltersAndRender();
    }

    function toggleMainCustomTimeFilter() {
      if (selectedTimeSlot === 'custom') {
        setDepartureTimeFilter('all');
      } else {
        setDepartureTimeFilter('custom');
      }
    }

    function applyMainCustomTimeFilter() {
      const sEl = document.getElementById('mainFilterTimeStart');
      const eEl = document.getElementById('mainFilterTimeEnd');
      if (sEl) filterCustomStart = sEl.value;
      if (eEl) filterCustomEnd = eEl.value;
      applyFiltersAndRender();
    }

    function normalizeMainFilterTime(inputEl, type) {
      const defaultH = (type === 'end') ? 24 : 0;
      inputEl.value = parseFlexibleTime(inputEl.value, defaultH);
      applyMainCustomTimeFilter();
    }

    function setMainFilterPreset(start, end) {
      const sEl = document.getElementById('mainFilterTimeStart');
      const eEl = document.getElementById('mainFilterTimeEnd');
      if (sEl) sEl.value = start;
      if (eEl) eEl.value = end;
      filterCustomStart = start;
      filterCustomEnd = end;
      applyFiltersAndRender();
    }

    function setDurationFilter(val) {
      selectedMaxDuration = val;
      document.getElementById('btnDurAll').classList.toggle('active', val === 'all');
      document.getElementById('btnDur5').classList.toggle('active', val === 5);
      document.getElementById('btnDur10').classList.toggle('active', val === 10);
      document.getElementById('btnDur15').classList.toggle('active', val === 15);
      document.getElementById('btnDur24').classList.toggle('active', val === 24);
      applyFiltersAndRender();
    }

    function selectAirlineFilter(airline) {
      selectedAirline = airline;
      buildAirlineFilterChips(allFlights);
      applyFiltersAndRender();
    }

    function setQuickFilter(type) {
      if (type === 'cheapest') {
        currentSort = 'price';
        setStopsFilter('all');
        setDurationFilter('all');
        setDepartureTimeFilter('all');
      } else if (type === 'fastest') {
        currentSort = 'duration';
        setStopsFilter(0);
        setDurationFilter('all');
        setDepartureTimeFilter('all');
      } else if (type === 'best') {
        currentSort = 'price';
        setStopsFilter('all');
        setDurationFilter(24);
        setDepartureTimeFilter('all');
      }
      
      document.getElementById('btnSortPrice').classList.toggle('active', currentSort === 'price');
      document.getElementById('btnSortDuration').classList.toggle('active', currentSort === 'duration');
      applyFiltersAndRender();
    }

    function applyFiltersAndRender() {
      let filtered = [...allFlights];

      // Stops filter (Booking.com style)
      if (selectedStops !== 'all') {
        if (selectedStops === 0) {
          filtered = filtered.filter(f => f.stops_count === 0 || f.is_direct);
        } else if (selectedStops === 1) {
          filtered = filtered.filter(f => f.stops_count === 1);
        } else if (selectedStops === 2) {
          filtered = filtered.filter(f => f.stops_count >= 2);
        }
      }

      // Max Duration filter (user adjustable)
      if (selectedMaxDuration !== 'all') {
        const maxMins = selectedMaxDuration * 60;
        filtered = filtered.filter(f => parseDurationMinutes(f.duration) <= maxMins);
      }

      // Departure Time filter (kalkış saati filtresi)
      if (selectedTimeSlot !== 'all') {
        filtered = filtered.filter(f => {
          if (!f.times) return true;
          const m = f.times.match(/^(\d{1,2}):(\d{2})/);
          if (!m) return true;
          const h = parseInt(m[1], 10);
          const mins = h * 60 + parseInt(m[2], 10);

          if (selectedTimeSlot === 'morning') return mins >= 6 * 60 && mins < 12 * 60;
          if (selectedTimeSlot === 'afternoon') return mins >= 12 * 60 && mins < 18 * 60;
          if (selectedTimeSlot === 'evening') return mins >= 18 * 60 && mins < 24 * 60;
          if (selectedTimeSlot === 'night') return mins >= 0 && mins < 6 * 60;
          if (selectedTimeSlot === 'custom') {
            const sMins = parseTimeToMinutes(filterCustomStart, 0);
            const eMins = parseTimeToMinutes(filterCustomEnd, 24 * 60);
            if (sMins <= eMins) {
              return mins >= sMins && mins <= eMins;
            } else {
              return mins >= sMins || mins <= eMins;
            }
          }
          return true;
        });
      }

      // Airline filter
      if (selectedAirline) {
        filtered = filtered.filter(f => f.airline.toLowerCase().includes(selectedAirline.toLowerCase()));
      }

      // Sort
      if (currentSort === 'price') {
        filtered.sort((a, b) => a.total_price_try - b.total_price_try);
      } else if (currentSort === 'duration') {
        filtered.sort((a, b) => parseDurationMinutes(a.duration) - parseDurationMinutes(b.duration));
      }

      document.getElementById('visibleFlightCount').innerText = filtered.length;
      renderFlightCards(filtered);
    }

    function renderFlightCards(flights) {
      const listEl = document.getElementById('liveFlightsList');
      if (!flights || !flights.length) {
        listEl.innerHTML = '<div style="text-align: center; padding: 40px; color: var(--text-muted); background: var(--surface-bg); border-radius: 14px;">Seçtiğiniz filtrelere uygun uçuş bulunamadı.</div>';
        return;
      }

      listEl.innerHTML = flights.map(f => `
        <div class="flight-card">
          <div>
            <div class="airline-tag">
              <span>✈️ ${escapeHtml(f.airline)}</span>
              ${f.airports ? `<span style="font-size: 13px; font-weight: 800; color: var(--badge-airport-text); background: var(--badge-airport-bg); padding: 2px 8px; border-radius: 6px; letter-spacing: 0.5px;">${escapeHtml(f.airports)}</span>` : ''}
              <span style="font-size: 11px; background: rgba(2, 132, 199, 0.12); color: var(--accent); padding: 2px 8px; border-radius: 4px; font-weight: 700;">${escapeHtml(f.stops)}</span>
              ${f.badge ? `<span style="font-size: 11px; background: rgba(22, 163, 74, 0.12); color: var(--green); padding: 2px 8px; border-radius: 4px; font-weight: 800; border: 1px solid rgba(22, 163, 74, 0.25);">${escapeHtml(f.badge)}</span>` : ''}
            </div>
            <div class="flight-meta">
              <span>⏰ Saatler: <strong>${escapeHtml(f.times)}</strong></span>
              <span>⏱️ Toplam Süre: <strong>${escapeHtml(f.duration)}</strong></span>
              ${f.layover_info ? `<span style="color: var(--orange); background: rgba(217, 119, 6, 0.1); padding: 2px 8px; border-radius: 4px; font-weight: 600;">📍 Aktarma: <strong>${escapeHtml(f.layover_info)}</strong></span>` : ''}
              <span>📍 Kaynak: <strong>${escapeHtml(f.source)}</strong></span>
              ${f.observed_at ? `<span title="${escapeHtml(f.observed_at)}">🕒 <strong>${escapeHtml(formatObservedAt(f.observed_at))}</strong></span>` : ''}
            </div>
            <div style="display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; align-items: center;">
              <a href="${safeUrl(f.google_flights_url)}" target="_blank" class="btn-link btn-google" style="font-weight: 800;">Google Flights'ta Aç ↗</a>
              <button class="btn-alarm-mini" onclick="openAlarmModalWithPrice(${f.total_price_try})">🔔 Alarm Kur</button>
            </div>
          </div>
          <div class="flight-price-box">
            <div class="flight-price">${f.total_price_try.toLocaleString('tr-TR')} ₺</div>
            <div class="flight-type">${escapeHtml(f.trip_type)}</div>
          </div>
        </div>
      `).join('');
    }

    /* Modal 1: Fiyat Alarmı Kur */
    function openAlarmModal() {
      const orig = document.getElementById('origin').value.trim();
      const dest = document.getElementById('destination').value.trim();
      if (!dest) {
        alert('Lütfen önce bir varış şehri aratın.');
        return;
      }

      const cheapest = allFlights.length > 0 ? allFlights[0].total_price_try : 30000;
      openAlarmModalWithPrice(cheapest, false);
    }

    function openAlarmModalWithPrice(price, isMultimodal = false) {
      window._activeAlarmPrice = price;
      window._activeAlarmIsMultimodal = isMultimodal;
      const orig = document.getElementById('origin').value.trim();
      const dest = document.getElementById('destination').value.trim();
      const depDate = document.getElementById('depDate').value;

      document.getElementById('alarmRouteTitle').innerText = `${orig} ➔ ${dest} (${depDate})${isMultimodal ? ' [Hibrit Rota]' : ''}`;
      document.getElementById('alarmCurrentPrice').innerText = `${price.toLocaleString('tr-TR')} ₺`;
      
      // Suggest target budget 10% lower
      const targetSuggestion = Math.round(price * 0.9 / 100) * 100;
      document.getElementById('inputTargetBudget').value = targetSuggestion;

      const chkMM = document.getElementById('chkWatchMultimodal');
      if (chkMM) chkMM.checked = true;

      const slotEl = document.getElementById('selectAlarmTimeSlot');
      if (slotEl) slotEl.value = 'all';
      const customBox = document.getElementById('customTimeRangeContainer');
      if (customBox) customBox.style.display = 'none';

      document.getElementById('alarmModal').style.display = 'flex';
    }

    function toggleCustomTimeInputs() {
      const slotEl = document.getElementById('selectAlarmTimeSlot');
      const customBox = document.getElementById('customTimeRangeContainer');
      if (slotEl && customBox) {
        customBox.style.display = slotEl.value === 'custom' ? 'block' : 'none';
        if (slotEl.value === 'custom') {
          updateAlarmTimeBadge();
        }
      }
    }

    function handleTimeInput(inputEl, type) {
      updateAlarmTimeBadge();
    }

    function normalizeTimeInput(inputEl, type) {
      const defaultH = (type === 'end') ? 24 : 0;
      inputEl.value = parseFlexibleTime(inputEl.value, defaultH);
      updateAlarmTimeBadge();
    }

    function updateAlarmTimeBadge() {
      const sEl = document.getElementById('inputCustomTimeStart');
      const eEl = document.getElementById('inputCustomTimeEnd');
      const badge = document.getElementById('alarmTimePreviewBadge');
      if (sEl && eEl && badge) {
        const s = sEl.value.trim() || '00:00';
        const e = eEl.value.trim() || '24:00';
        badge.innerText = `${s} – ${e}`;
      }
    }

    function setAlarmCustomPreset(start, end) {
      const sEl = document.getElementById('inputCustomTimeStart');
      const eEl = document.getElementById('inputCustomTimeEnd');
      if (sEl) sEl.value = start;
      if (eEl) eEl.value = end;
      updateAlarmTimeBadge();
    }

    function closeAlarmModal() {
      window._activeAlarmPrice = null;
      window._activeAlarmIsMultimodal = false;
      document.getElementById('alarmModal').style.display = 'none';
    }

    async function saveBudgetWatch() {
      const orig = document.getElementById('origin').value.trim();
      const dest = document.getElementById('destination').value.trim();
      const depDate = document.getElementById('depDate').value;
      const retDate = document.getElementById('retDate').value;
      const targetBudget = parseFloat(document.getElementById('inputTargetBudget').value);
      const maxTransfers = parseInt(document.getElementById('selectMaxTransfers').value);
      const timeSlot = document.getElementById('selectAlarmTimeSlot') ? document.getElementById('selectAlarmTimeSlot').value : 'all';

      let winStart = null;
      let winEnd = null;
      if (timeSlot === 'custom') {
        const sEl = document.getElementById('inputCustomTimeStart');
        const eEl = document.getElementById('inputCustomTimeEnd');
        winStart = parseFlexibleTime(sEl ? sEl.value : '00:00', 0);
        winEnd = parseFlexibleTime(eEl ? eEl.value : '24:00', 24);
      }

      if (!targetBudget || targetBudget <= 0) {
        alert('Lütfen geçerli bir hedef bütçe tutarı girin.');
        return;
      }

      const currentPrice = window._activeAlarmPrice || (allFlights.length > 0 ? allFlights[0].total_price_try : targetBudget);
      const includeMultimodal = document.getElementById('chkWatchMultimodal') ? document.getElementById('chkWatchMultimodal').checked : true;

      try {
        const res = await fetch('/api/watches', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            origin: orig,
            destination: dest,
            target_budget_try: targetBudget,
            departure_date: depDate,
            return_date: currentTripType === 'roundtrip' ? retDate : null,
            trip_type: currentTripType,
            max_transfers: maxTransfers,
            departure_time_slot: timeSlot,
            time_window_start: winStart,
            time_window_end: winEnd,
            current_price: currentPrice,
            include_multimodal: includeMultimodal
          })
        });

        if (res.ok) {
          closeAlarmModal();
          let slotLabel = '';
          if (timeSlot === 'morning') slotLabel = ' (Sabah 06:00 – 12:00)';
          else if (timeSlot === 'afternoon') slotLabel = ' (Öğle 12:00 – 18:00)';
          else if (timeSlot === 'evening') slotLabel = ' (Akşam 18:00 – 24:00)';
          else if (timeSlot === 'night') slotLabel = ' (Gece 00:00 – 06:00)';
          else if (timeSlot === 'custom') slotLabel = ` (${winStart} – ${winEnd} arası)`;

          alert(`✅ Bütçe Alarmı Kuruldu!\n${orig} ➔ ${dest} (${depDate})${slotLabel} için bilet fiyatı ${targetBudget.toLocaleString('tr-TR')} ₺ altına düştüğünde fırsat bildirimi üretilecektir.\n\n🛡️ Bu alarm o aralıktaki tüm uçuşları ve sonradan eklenecek yeni uçakları da kapsar!`);
          loadWatchesCount();
        } else {
          alert(await readApiErrorMessage(res, 'Alarm kaydedilirken sunucu hatası oluştu.'));
        }
      } catch (e) {
        alert('Bağlantı hatası oluştu.');
      }
    }

    /* Modal 2: Takiplerim Paneli */
    async function openWatchesModal() {
      document.getElementById('watchlistModal').style.display = 'flex';
      const container = document.getElementById('watchlistContent');
      container.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-muted);">Yükleniyor...</div>';

      try {
        const res = await fetch('/api/watches');
        const data = await res.json();
        const watches = data.watches || [];
        updateWatchBadge(watches.length);

        if (!watches.length) {
          container.innerHTML = '<div style="text-align: center; padding: 40px; color: var(--text-muted);">Henüz kayıtlı bütçe takibiniz yok. Arama sonuçları üzerinden "Bütçe Alarmı Kur" butonuna basarak takip başlatabilirsiniz.</div>';
          return;
        }

        window._watchList = watches;
        container.innerHTML = watches.map(w => {
          if (isTripWatch(w)) return renderTripWatchCard(w);
          const latest = w.latest_price || 0;
          const target = w.target_budget_try || 0;
          const isTriggered = latest > 0 && latest <= target;
          const transBadge = w.max_transfers === 0
            ? `<span style="font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; background: rgba(22, 163, 74, 0.15); color: var(--green); font-weight: 700;">⚡ Sadece Aktarmasız</span>`
            : '';

          return `
            <div class="watch-card">
              <div>
                <div style="font-size: 15px; font-weight: 800; color: var(--text);">
                  ${escapeHtml(w.origin)} ➔ ${escapeHtml(w.destination)} ${w.departure_date ? `<span style="font-size: 12px; color: var(--text-muted); font-weight: 600;">(${escapeHtml(w.departure_date)})</span>` : ''}
                  <span style="font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; background: rgba(56, 189, 248, 0.15); color: #38bdf8;">${w.trip_type === 'oneway' ? 'Tek Yön' : 'Gidiş-Dönüş'}</span>
                  ${(() => {
                    let slotText = '';
                    if (w.departure_time_slot === 'morning') slotText = '🌅 Sabah (06-12)';
                    else if (w.departure_time_slot === 'afternoon') slotText = '☀️ Öğle (12-18)';
                    else if (w.departure_time_slot === 'evening') slotText = '🌙 Akşam (18-24)';
                    else if (w.departure_time_slot === 'night') slotText = '🌌 Gece (00-06)';
                    else if (w.departure_time_slot === 'custom' && (w.time_window_start || w.time_window_end)) {
                      slotText = `⏰ ${escapeHtml(w.time_window_start || '00:00')} – ${escapeHtml(w.time_window_end || '24:00')}`;
                    }
                    return slotText ? `<span style="font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; background: rgba(124, 58, 237, 0.15); color: var(--purple); font-weight: 700;">${slotText}</span>` : '';
                  })()}
                  ${transBadge}
                </div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">
                  Hedef Bütçe: <strong style="color: var(--green);">${target.toLocaleString('tr-TR')} ₺</strong> · Son Fiyat: <strong>${latest ? latest.toLocaleString('tr-TR') + ' ₺' : 'Henüz taranmadı'}</strong>
                </div>
                <div style="margin-top: 6px;">
                  ${isTriggered
                    ? '<span style="font-size: 11px; font-weight: 800; background: rgba(16, 185, 129, 0.2); color: #10b981; padding: 2px 8px; border-radius: 4px;">🟢 FIRSAT: Hedef Bütçenin Altında!</span>'
                    : '<span style="font-size: 11px; font-weight: 700; background: rgba(245, 158, 11, 0.2); color: #fbbf24; padding: 2px 8px; border-radius: 4px;">🟡 Takipte (Fiyat düşüşü bekleniyor)</span>'
                  }
                </div>
              </div>
              <div style="display: flex; gap: 8px; align-items: center;">
                <button class="filter-btn" onclick="quickSearchFromWatch(${jsArg(w.origin)}, ${jsArg(w.destination)}, ${jsArg(w.departure_date || defaultTripDates(localTodayIso()).depDate)}, ${jsArg(w.trip_type || 'oneway')})">🔍 Şimdi Ara</button>
                <button class="btn-delete-watch" onclick="deleteWatch(${jsArg(w.id)})">🗑️ Sil</button>
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {
        container.innerHTML = '<div style="color: #f87171; text-align: center; padding: 20px;">Takipler yüklenirken hata oluştu.</div>';
      }
    }

    function closeWatchesModal() {
      document.getElementById('watchlistModal').style.display = 'none';
    }

    async function deleteWatch(id) {
      if (!confirm('Bu fiyat takibini silmek istediğinize emin misiniz?')) return;
      try {
        const res = await fetch('/api/watches/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id })
        });
        if (res.ok) {
          openWatchesModal(); // Refresh modal
          loadWatchesCount();
        }
      } catch (e) {
        alert('Silme sırasında hata oluştu.');
      }
    }

    function quickSearchFromWatch(origin, destination, depDate, tripType) {
      closeWatchesModal();
      document.getElementById('origin').value = origin;
      document.getElementById('destination').value = destination;
      if (depDate) document.getElementById('depDate').value = depDate;
      setTripType(tripType);
      searchLiveFlights();
    }

    async function loadWatchesCount() {
      try {
        const res = await fetch('/api/watches');
        const data = await res.json();
        updateWatchBadge((data.watches || []).length);
      } catch (e) {
        // ignore
      }
    }

    function updateWatchBadge(count) {
      const el = document.getElementById('headerWatchCount');
      if (el) el.innerText = count;
    }

    /* Manual scan trigger */
    async function triggerManualScan() {
      const btn = document.getElementById('btnScanAllNow');
      const banner = document.getElementById('scanStatusBanner');
      btn.disabled = true;
      btn.innerHTML = '<span>⏳</span> Taranıyor... Lütfen bekleyin';
      banner.style.display = 'block';
      banner.style.background = 'rgba(56, 189, 248, 0.15)';
      banner.style.color = 'var(--accent)';
      banner.innerText = 'Tüm aktif rotalar taranıyor ve Google Flights ile canlı fiyatlar taranıyor...';

      try {
        const res = await fetch('/api/tracker/scan-now', { method: 'POST' });
        const data = await res.json();
        const results = data.results || [];
        const triggered = results.filter(r => r.triggered);

        banner.style.background = 'rgba(16, 185, 129, 0.2)';
        banner.style.color = '#10b981';
        banner.innerHTML = `✅ Tarama tamamlandı! <strong>${results.length}</strong> rota güncellendi. ${triggered.length ? `<strong>${triggered.length} fırsat alarmı tetiklendi!</strong>` : 'Şu an hedef bütçe altında yeni düşüş yok.'}`;

        await openWatchesModal();
        await loadAlerts();
      } catch (e) {
        banner.style.background = 'rgba(239, 68, 68, 0.2)';
        banner.style.color = '#f87171';
        banner.innerText = 'Tarama sırasında bir hata oluştu.';
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>⚡</span> Tüm Takipleri Şimdi Otomatik Tara';
      }
    }

    /* Modal 3: Telegram Settings */
    async function openTelegramModal() {
      document.getElementById('telegramModal').style.display = 'flex';
      const msgEl = document.getElementById('tgStatusMsg');
      msgEl.style.display = 'none';

      try {
        const res = await fetch('/api/settings/telegram');
        const data = await res.json();
        if (data.has_token) {
          document.getElementById('tgBotToken').placeholder = `Kayıtlı: ${data.masked_token}`;
          document.getElementById('tgBotToken').value = '';
        } else {
          document.getElementById('tgBotToken').placeholder = 'Örn: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ...';
          document.getElementById('tgBotToken').value = '';
        }
        document.getElementById('tgChatId').value = data.chat_id || '';
        document.getElementById('tgEnabled').checked = data.enabled !== false;
      } catch (e) {
        console.error('Failed to load telegram settings:', e);
      }
    }

    function closeTelegramModal() {
      document.getElementById('telegramModal').style.display = 'none';
    }

    async function saveTelegramSettings() {
      const botToken = document.getElementById('tgBotToken').value.trim();
      const chatId = document.getElementById('tgChatId').value.trim();
      const enabled = document.getElementById('tgEnabled').checked;
      const msgEl = document.getElementById('tgStatusMsg');

      try {
        const res = await fetch('/api/settings/telegram', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bot_token: botToken, chat_id: chatId, enabled, test: false })
        });
        const data = await res.json();
        msgEl.style.display = 'block';
        msgEl.style.background = 'rgba(16, 185, 129, 0.2)';
        msgEl.style.color = '#10b981';
        msgEl.innerText = '✅ Ayarlar başarıyla kaydedildi.';
        setTimeout(() => closeTelegramModal(), 1200);
      } catch (e) {
        msgEl.style.display = 'block';
        msgEl.style.background = 'rgba(239, 68, 68, 0.2)';
        msgEl.style.color = '#f87171';
        msgEl.innerText = 'Kaydetme sırasında hata oluştu.';
      }
    }

    async function testTelegram() {
      const btn = document.getElementById('btnTestTg');
      const botToken = document.getElementById('tgBotToken').value.trim();
      const chatId = document.getElementById('tgChatId').value.trim();
      const enabled = document.getElementById('tgEnabled').checked;
      const msgEl = document.getElementById('tgStatusMsg');

      btn.disabled = true;
      btn.innerText = '⏳ Gönderiliyor...';
      msgEl.style.display = 'none';

      try {
        const res = await fetch('/api/settings/telegram', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bot_token: botToken, chat_id: chatId, enabled, test: true })
        });
        const data = await res.json();
        msgEl.style.display = 'block';
        if (data.test_result && data.test_result.success) {
          msgEl.style.background = 'rgba(16, 185, 129, 0.2)';
          msgEl.style.color = '#10b981';
          msgEl.innerText = '✅ ' + data.test_result.message;
        } else {
          msgEl.style.background = 'rgba(239, 68, 68, 0.2)';
          msgEl.style.color = '#f87171';
          msgEl.innerText = '❌ ' + (data.test_result ? data.test_result.message : 'Bağlantı kurulamadı.');
        }
      } catch (e) {
        msgEl.style.display = 'block';
        msgEl.style.background = 'rgba(239, 68, 68, 0.2)';
        msgEl.style.color = '#f87171';
        msgEl.innerText = '❌ Test isteği gönderilirken hata oluştu.';
      } finally {
        btn.disabled = false;
        btn.innerText = '🔔 Test Bildirimi Gönder';
      }
    }

    /* Modal 4: Alerts (Fırsatlar) Paneli */
    async function openAlertsModal() {
      document.getElementById('alertsModal').style.display = 'flex';
      const container = document.getElementById('alertsContent');
      container.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-muted);">Yükleniyor...</div>';

      try {
        const res = await fetch('/api/alerts');
        const data = await res.json();
        const alerts = data.alerts || [];

        if (!alerts.length) {
          container.innerHTML = '<div style="text-align: center; padding: 40px; color: var(--text-muted);">Henüz yakalanmış bir fırsat bildirimi yok. Arka plan motoru hedef bütçenizin altında bilet yakaladığında burada listelenecektir.</div>';
          return;
        }

        container.innerHTML = alerts.map(a => {
          const dateStr = a.timestamp ? new Date(a.timestamp).toLocaleString('tr-TR') : '';
          if (a.type === 'system') {
            return `
              <div class="watch-card" style="border-style: dashed; border-color: rgba(217, 119, 6, 0.55); background: rgba(217, 119, 6, 0.06); margin-bottom: 10px;">
                <div style="font-size: 13px; font-weight: 800; color: var(--orange);">⚙️ Sistem bildirimi</div>
                <div style="font-size: 13px; color: var(--text); margin-top: 6px;">${escapeHtml(a.message || '')}</div>
                <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">${escapeHtml(dateStr)}</div>
              </div>
            `;
          }
          const isMultiModal = a.type === 'multimodal';
          const typeBadge = isMultiModal
            ? `<span style="font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; background: rgba(16, 185, 129, 0.2); color: #10b981; font-weight: 700;">🚆 Hibrit (Uçuş + Tren/Otobüs)</span>`
            : `<span style="font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; background: rgba(16, 185, 129, 0.2); color: #10b981;">${escapeHtml(a.airline || '')} · ${escapeHtml(a.trip_type || '')}</span>`;

          const savingPart = a.saving
            ? `<span style="font-weight: 800; color: #fbbf24;">+${Math.round(a.saving).toLocaleString('tr-TR')} ₺ Tasarruf</span>`
            : '';
          const directSavingPart = (isMultiModal && a.savings_vs_direct > 0)
            ? ` · <span style="color: #10b981; font-weight: 700;">(Uçuşa göre +${Math.round(a.savings_vs_direct).toLocaleString('tr-TR')} ₺ kazanç)</span>`
            : '';
          const discountPart = a.discount_pct ? ` (%${escapeHtml(a.discount_pct)} indirim)` : '';

          const stepsHtml = (isMultiModal && a.segments_summary)
            ? `<pre style="font-size: 11px; margin: 6px 0 0 0; white-space: pre-wrap; font-family: inherit; color: var(--text-muted); background: var(--surface-bg); padding: 6px 10px; border-radius: 6px; border: 1px solid var(--card-border);">${escapeHtml(a.segments_summary)}</pre>`
            : '';

          return `
            <div class="watch-card" style="border-color: rgba(16, 185, 129, 0.4); background: rgba(16, 185, 129, 0.04); display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px;">
              <div style="flex: 1;">
                <div style="font-size: 15px; font-weight: 800; color: var(--text);">
                  ${escapeHtml(a.origin || '')} ➔ ${escapeHtml(a.destination || '')} ${a.departure_date ? `<span style="font-size: 12px; color: var(--text-muted); font-weight: 600;">(${escapeHtml(a.departure_date)})</span>` : ''}
                  ${typeBadge}
                </div>
                <div style="font-size: 13px; color: var(--text-muted); margin-top: 6px;">
                  Yakalanan Fiyat: <strong style="color: #10b981; font-size: 16px;">${Math.round(a.current_price || 0).toLocaleString('tr-TR')} ₺</strong>
                  · Hedef Bütçeniz: <strong style="color: var(--text);">${Math.round(a.target_budget || 0).toLocaleString('tr-TR')} ₺</strong>
                </div>
                <div style="font-size: 12px; margin-top: 4px;">
                  🔥 ${savingPart}${discountPart}${directSavingPart}
                  ${a.telegram_sent ? '· <span style="color: #38bdf8;">📲 Telegram iletildi</span>' : ''}
                </div>
                ${stepsHtml}
                <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">
                  ${a.duration ? `⏱️ ${escapeHtml(a.duration)} · ` : ''}${escapeHtml(dateStr)}
                </div>
              </div>
              <div style="display: flex; flex-direction: column; gap: 6px; align-items: flex-end;">
                ${a.google_url ? `<a href="${safeUrl(a.google_url)}" target="_blank" class="btn-link btn-google" style="margin-top:0;">🔗 Google Flights</a>` : ''}
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {
        container.innerHTML = '<div style="color: #f87171; text-align: center; padding: 20px;">Bildirimler yüklenirken hata oluştu.</div>';
      }
    }

    function closeAlertsModal() {
      document.getElementById('alertsModal').style.display = 'none';
    }

    async function clearAllAlerts() {
      if (!confirm('Tüm bildirim geçmişini silmek istediğinize emin misiniz?')) return;
      try {
        await fetch('/api/alerts/clear', { method: 'POST' });
        openAlertsModal();
        loadAlerts();
      } catch (e) {
        alert('Bildirimler silinirken hata oluştu.');
      }
    }

    async function loadAlerts() {
      try {
        const res = await fetch('/api/alerts');
        const data = await res.json();
        const alerts = data.alerts || [];
        const btnHeader = document.getElementById('btnAlertsHeader');
        const badgeHeader = document.getElementById('headerAlertCount');
        const banner = document.getElementById('opportunityBanner');
        const bannerText = document.getElementById('opportunityText');

        // System notices (type "system") have no price, so the opportunity banner uses the newest deal alert
        // and the header count only counts deal alerts. The button stays visible while any notice exists, so the modal is still reachable.
        const topAlert = alerts.find(a => a.type !== 'system');
        const dealCount = alerts.filter(a => a && a.type !== 'system').length;
        if (alerts.length > 0) {
          btnHeader.style.display = 'inline-flex';
          badgeHeader.innerText = dealCount;
        } else {
          btnHeader.style.display = 'none';
        }
        if (topAlert) {
          banner.style.display = 'flex';
          bannerText.innerText = `${topAlert.origin} ➔ ${topAlert.destination}: ${topAlert.current_price.toLocaleString('tr-TR')} ₺ (Hedef: ${topAlert.target_budget.toLocaleString('tr-TR')} ₺ · +${topAlert.saving.toLocaleString('tr-TR')} ₺ Tasarruf)`;
        } else {
          banner.style.display = 'none';
        }
      } catch (e) {
        // ignore
      }
    }

    /* ---- Gezi Planı (kurallı arama) ---- */
    const TRIP_PLANNER_KEY = 'travelplan_trip_planner_v1';
    const TRIP_MAX_DATES = 7;
    const TRIP_BUDGET_MAX = 60;
    const TRIP_BUDGET_DEFAULT = 20;
    const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
    const TRIP_FIELD_IDS = {
      origin: 'tpOrigin', destination: 'tpDestination',
      dateStart: 'tpDateStart', dateEnd: 'tpDateEnd',
      arriveDate: 'tpArriveDate', arriveTime: 'tpArriveTime',
      afterDate: 'tpAfterDate', afterTime: 'tpAfterTime',
      via: 'tpVia', directBudget: 'tpDirectBudget', sideBudget: 'tpSideBudget',
      maxStops: 'tpMaxStops', flightBudget: 'tpFlightBudget', flex: 'tpFlex',
      returnDateStart: 'tpReturnDateStart', returnDateEnd: 'tpReturnDateEnd',
      returnArriveDate: 'tpReturnArriveDate', returnArriveTime: 'tpReturnArriveTime',
      returnVia: 'tpReturnVia', minStayHours: 'tpMinStayHours'
    };
    /* Used when a saved form or preset has no value for a field (older saves stay valid) */
    const TRIP_FIELD_DEFAULTS = {
      arriveTime: '23:59', returnArriveTime: '23:59',
      maxStops: '2', flightBudget: String(TRIP_BUDGET_DEFAULT), flex: '0'
    };
    const TRIP_BAG_VALUES = ['small', 'cabin', 'checked'];
    /* "Sadece gerçek veriler": its own saved switch (per browser, default off) */
    const TRIP_REAL_ONLY_KEY = 'travelplan_trip_real_only_v1';
    const TRIP_REAL_ONLY_EMPTY_TEXT = 'Gerçek veriyle seçenek bulunamadı — butonu kapatarak tahmini rotaları (etiketli) görebilirsin';
    let tripRealOnly = false;
    /* Unknown-price labels: the part names of cost_breakdown.unknown_parts / option.unknown_price_parts */
    const TRIP_UNKNOWN_PART_LABELS = { baggage: 'bagaj', transfers: 'transfer', hotel: 'otel' };
    const TRIP_PRESETS = {
      outbound: { origin: 'Atina', destination: 'Prag', dateStart: '2026-10-27', dateEnd: '2026-10-28', arriveDate: '2026-10-28', arriveTime: '12:00', afterDate: '', afterTime: '', via: '', sideRoutes: true, directBudget: '10000', sideBudget: '7000' },
      inbound: { origin: 'Prag', destination: 'İstanbul', dateStart: '2026-10-31', dateEnd: '2026-10-31', arriveDate: '2026-10-31', arriveTime: '23:59', afterDate: '', afterTime: '', via: 'Yunanistan', sideRoutes: true, directBudget: '10000', sideBudget: '7000' }
    };

    /* ISO "YYYY-MM-DD" -> UTC midnight in ms, or null when the date does not exist */
    function isoDateToUtcMs(iso) {
      const m = ISO_DATE_RE.exec(String(iso || '').trim());
      if (!m) return null;
      const ms = Date.UTC(+m[1], +m[2] - 1, +m[3]);
      return new Date(ms).getUTCDate() === +m[3] ? ms : null;
    }

    /* "YYYY-MM-DD" + n days -> "YYYY-MM-DD" ('' when invalid). UTC math avoids timezone drift. */
    function addDaysIso(iso, n) {
      const ms = isoDateToUtcMs(iso);
      if (ms === null) return '';
      return new Date(ms + Math.trunc(Number(n) || 0) * 86400000).toISOString().slice(0, 10);
    }

    /* ---- Varsayılan tarihler (bugüne göre hesaplanır, sabit tarih yok) ---- */
    const DEFAULT_DEP_OFFSET_DAYS = 14;   // gidiş = bugün + 14
    const DEFAULT_RET_OFFSET_DAYS = 21;   // dönüş = bugün + 21
    const EXAMPLE_MIN_OFFSET_DAYS = 7;    // geçmişte kalan örnek tarih en az bugün + 7'ye kaydırılır

    /* Local calendar date -> "YYYY-MM-DD" (local getters, not toISOString, so the day never shifts) */
    function localTodayIso(now) {
      const d = now instanceof Date ? now : new Date();
      const pad = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }

    /* Saved ISO date is kept when it is today or later; empty, invalid or past -> fallback */
    function futureOrDefaultIso(savedIso, fallbackIso, todayIso) {
      const ms = isoDateToUtcMs(savedIso);
      const todayMs = isoDateToUtcMs(todayIso);
      if (ms === null || todayMs === null || ms < todayMs) return fallbackIso;
      return String(savedIso).trim();
    }

    /* Default departure / return dates for the classic search: {depDate, retDate} */
    function defaultTripDates(todayIso) {
      return {
        depDate: addDaysIso(todayIso, DEFAULT_DEP_OFFSET_DAYS),
        retDate: addDaysIso(todayIso, DEFAULT_RET_OFFSET_DAYS)
      };
    }

    /* Example preset whose start date is in the past is moved to the same weekday on or after today+7.
       All of its dates move by the same number of days. Returns {preset, shifted}. */
    function shiftPresetToFuture(preset, todayIso) {
      const startMs = isoDateToUtcMs(preset.dateStart);
      const todayMs = isoDateToUtcMs(todayIso);
      if (startMs === null || todayMs === null || startMs >= todayMs) return { preset, shifted: false };
      const minIso = addDaysIso(todayIso, EXAMPLE_MIN_OFFSET_DAYS);
      const minMs = isoDateToUtcMs(minIso);
      const weekdayGap = (new Date(startMs).getUTCDay() - new Date(minMs).getUTCDay() + 7) % 7;
      const newStartMs = minMs + weekdayGap * 86400000;
      const shiftDays = Math.round((newStartMs - startMs) / 86400000);
      const move = s => (s ? addDaysIso(s, shiftDays) : s);
      return {
        preset: Object.assign({}, preset, {
          dateStart: move(preset.dateStart),
          dateEnd: move(preset.dateEnd),
          arriveDate: move(preset.arriveDate),
          afterDate: move(preset.afterDate)
        }),
        shifted: true
      };
    }

    /* Trip-planner date fields that are not in the past (empty when they are); used when restoring a saved form */
    function keepFutureTripDates(saved, todayIso) {
      const out = Object.assign({}, saved);
      ['dateStart', 'dateEnd', 'arriveDate', 'afterDate', 'returnDateStart', 'returnDateEnd', 'returnArriveDate'].forEach(k => {
        const v = String(out[k] || '').trim();
        if (!v) return;
        const ms = isoDateToUtcMs(v);
        const todayMs = isoDateToUtcMs(todayIso);
        if (ms === null || todayMs === null || ms < todayMs) out[k] = '';
      });
      return out;
    }

    /* Start date + optional end date (inclusive) -> list of ISO dates, at most TRIP_MAX_DATES */
    function expandTripDates(startIso, endIso) {
      const start = String(startIso || '').trim();
      const end = String(endIso || '').trim();
      const startMs = isoDateToUtcMs(start);
      if (startMs === null) return { dates: [], error: 'Lütfen bir kalkış tarihi seçin.' };
      if (!end) return { dates: [start], error: '' };
      const endMs = isoDateToUtcMs(end);
      if (endMs === null) return { dates: [], error: 'Kalkış bitiş tarihi geçersiz.' };
      if (endMs < startMs) return { dates: [], error: 'Kalkış bitiş tarihi, başlangıç tarihinden önce olamaz.' };
      const count = Math.round((endMs - startMs) / 86400000) + 1;
      if (count > TRIP_MAX_DATES) {
        return { dates: [], error: `En fazla ${TRIP_MAX_DATES} kalkış tarihi seçilebilir (seçtiğiniz aralıkta ${count} gün var).` };
      }
      const dates = [];
      for (let i = 0; i < count; i++) dates.push(addDaysIso(start, i));
      return { dates, error: '' };
    }

    /* "2026-10-27T19:30" -> { date: "2026-10-27", time: "19:30" }; empty strings when not parseable */
    function splitTripDateTime(v) {
      const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(String(v || '').trim());
      return m ? { date: m[1], time: m[2] } : { date: '', time: '' };
    }

    /* "2026-10-27T19:30" -> "27 Eki Sal 19:30"; '' when the value is not a date-time */
    function formatTrDateTime(isoDt) {
      const p = splitTripDateTime(isoDt);
      if (!p.date) return '';
      const day = formatTrShortDate(p.date, 0);
      return day ? `${day} ${p.time}` : '';
    }

    /* Like formatTrDateTime, but keeps the raw text (or '--') so no server value silently disappears */
    function formatTrDateTimeOrRaw(v) {
      const s = String(v === null || v === undefined ? '' : v).trim();
      return formatTrDateTime(s) || s || '--';
    }

    /* Form date + time -> "YYYY-MM-DDTHH:MM"; '' when no date is given */
    function combineTripDateTime(dateIso, time, defaultTime) {
      const d = String(dateIso || '').trim();
      if (!d) return '';
      const t = String(time || '').trim() || defaultTime;
      return `${d}T${t}`;
    }

    /* Budget input -> positive number, or null when empty or invalid */
    function parseTripBudget(raw) {
      const s = String(raw === null || raw === undefined ? '' : raw).trim();
      if (!s) return null;
      const n = Number(s);
      return Number.isFinite(n) && n > 0 ? n : null;
    }

    /* Integer input -> integer clamped to [min, max]; empty or non-numeric -> def */
    function parseIntInRange(raw, min, max, def) {
      const s = String(raw === null || raw === undefined ? '' : raw).trim();
      if (!s) return def;
      const n = Number(s);
      if (!Number.isFinite(n)) return def;
      return Math.min(max, Math.max(min, Math.round(n)));
    }

    /* Non-negative number input -> number, or null when empty or invalid */
    function parseNonNegativeNumber(raw) {
      const s = String(raw === null || raw === undefined ? '' : raw).trim();
      if (!s) return null;
      const n = Number(s);
      return Number.isFinite(n) && n >= 0 ? n : null;
    }

    /* "Budapeşte, Viyana" -> ["Budapeşte", "Viyana"]: order kept, blanks dropped */
    function parseViaList(raw) {
      return String(raw === null || raw === undefined ? '' : raw)
        .split(',').map(s => s.trim()).filter(Boolean);
    }

    /* Hours typed in a min-stay row -> whole minutes, or null when empty or invalid */
    function parseHoursToMins(raw) {
      const n = parseNonNegativeNumber(raw);
      return n === null ? null : Math.round(n * 60);
    }

    /* "Budapeşte:720;Viyana:60" -> [{place: "Budapeşte", mins: 720}, {place: "Viyana", mins: 60}]. Invalid entries are dropped. */
    function parseMinStay(raw) {
      const out = [];
      String(raw === null || raw === undefined ? '' : raw).split(';').forEach(part => {
        const idx = part.lastIndexOf(':');
        if (idx < 0) return;
        const place = part.slice(0, idx).trim();
        const mins = Number(part.slice(idx + 1).trim());
        if (!place || !Number.isFinite(mins) || mins < 0) return;
        out.push({ place, mins: Math.round(mins) });
      });
      return out;
    }

    /* [{place, mins}] -> "Budapeşte:720;Viyana:60". Blank or invalid entries are skipped; ':' and ';' are removed from names. */
    function serializeMinStay(items) {
      return (Array.isArray(items) ? items : []).map(it => {
        const place = String(it && it.place !== undefined && it.place !== null ? it.place : '').replace(/[:;]/g, ' ').trim();
        if (!place || !it || !hasNumber(it.mins) || it.mins < 0) return null;
        return `${place}:${Math.round(it.mins)}`;
      }).filter(Boolean).join(';');
    }

    /* [{place, mins}] -> {place: mins} (the shape the watch payload uses) */
    function minStayToObject(items) {
      const obj = {};
      (Array.isArray(items) ? items : []).forEach(it => {
        if (it && it.place && hasNumber(it.mins)) obj[it.place] = it.mins;
      });
      return obj;
    }

    /* {place: mins} -> "Budapeşte 12 sa, Viyana 1 sa" for watch cards */
    function formatMinStayText(obj) {
      if (!isPlainObject(obj)) return '';
      return Object.keys(obj).filter(k => hasNumber(obj[k]))
        .map(k => `${k} ${formatHoursShort(obj[k])}`).join(', ');
    }

    /* Minutes -> "12 sa" or "1,5 sa" */
    function formatHoursShort(mins) {
      if (!hasNumber(mins)) return '--';
      return `${(Math.round(mins / 6) / 10).toLocaleString('tr-TR')} sa`;
    }

    function isPlainObject(v) {
      return !!v && typeof v === 'object' && !Array.isArray(v);
    }

    /* Bag radio value -> backend flags */
    function bagFlags(bag) {
      return { cabin_bag: bag === 'small' ? 0 : 1, checked_bag: bag === 'checked' ? 1 : 0 };
    }

    function flagOn(v) { return v === 1 || v === true || v === '1'; }
    function flagOff(v) { return v === 0 || v === false || v === '0'; }

    /* Saved backend flags -> bag radio value (missing flags -> "small": the planner's default, small bag only) */
    function bagFromFlags(cabin, checked) {
      if (flagOn(checked)) return 'checked';
      return flagOn(cabin) ? 'cabin' : 'small';
    }

    /* Backend flags -> label for watch cards; '' when neither flag is present */
    function tripBagLabel(cabin, checked) {
      const absent = v => v === undefined || v === null;
      if (absent(cabin) && absent(checked)) return '';
      if (flagOn(checked)) return 'kabin + valiz';
      return flagOff(cabin) ? 'sadece küçük çanta' : 'kabin bagajı';
    }

    /* Cost breakdown keys, Turkish labels and the spellings that may appear in estimated_parts */
    const TRIP_COST_PARTS = [
      { key: 'tickets', label: 'Bilet', aliases: ['tickets', 'ticket', 'bilet'] },
      { key: 'baggage', label: 'bagaj', aliases: ['baggage', 'luggage', 'bagaj'] },
      { key: 'transfers', label: 'transfer', aliases: ['transfers', 'transfer', 'transport'] },
      { key: 'hotel', label: 'otel', aliases: ['hotel', 'otel', 'accommodation'] }
    ];

    /* cost_breakdown -> { parts: [{label, amount, estimated}], total, totalEstimated } or null.
       Missing parts are left out; the total is the server value, or the sum of the parts. */
    function costBreakdownParts(cb) {
      if (!isPlainObject(cb)) return null;
      const est = (Array.isArray(cb.estimated_parts) ? cb.estimated_parts : [])
        .map(x => String(x).trim().toLowerCase());
      const parts = [];
      TRIP_COST_PARTS.forEach(p => {
        if (!hasNumber(cb[p.key])) return;
        parts.push({ label: p.label, amount: cb[p.key], estimated: p.aliases.some(a => est.indexOf(a) >= 0) });
      });
      const sum = parts.reduce((acc, p) => acc + p.amount, 0);
      const total = hasNumber(cb.total) ? cb.total : (parts.length ? sum : null);
      if (total === null) return null;
      const totalEstimated = est.indexOf('total') >= 0 || parts.some(p => p.estimated);
      return { parts, total, totalEstimated };
    }

    /* "Bilet 1.200 ₺ + bagaj ~300 ₺ + transfer ~150 ₺ + otel ~800 ₺ = Toplam ~2.450 ₺" ('' when nothing to show) */
    function formatCostBreakdownText(cb) {
      const info = costBreakdownParts(cb);
      if (!info) return '';
      // An incomplete total is a lower bound ("≥"); an unknown part is named, never shown as a number
      const lower = isPlainObject(cb) && cb.price_complete === false ? '≥ ' : '';
      const totalText = `${lower}${info.totalEstimated ? '~' : ''}${formatTry(info.total)}`;
      const unknown = tripListItems(isPlainObject(cb) ? cb.unknown_parts : null)
        .filter(k => Object.prototype.hasOwnProperty.call(TRIP_UNKNOWN_PART_LABELS, k))
        .map(k => `${TRIP_UNKNOWN_PART_LABELS[k]} fiyatı yok`);
      const tail = unknown.length ? ` · ${unknown.join(' · ')}` : '';
      if (!info.parts.length) return `Toplam ${totalText}${tail}`;
      const items = info.parts.map(p => `${p.label} ${p.estimated ? '~' : ''}${formatTry(p.amount)}`);
      return `${items.join(' + ')} = Toplam ${totalText}${tail}`;
    }

    /* Values that may be strings, numbers or objects (name/label/place) -> trimmed strings */
    function tripListText(v) {
      if (typeof v === 'string') return v.trim();
      if (typeof v === 'number') return String(v);
      if (isPlainObject(v)) return String(v.label || v.name || v.place || v.text || '').trim();
      return '';
    }

    function tripListItems(arr) {
      return (Array.isArray(arr) ? arr : []).map(tripListText).filter(Boolean);
    }

    const TRIP_RISK_LEVELS = {
      low: { text: '🟢 Düşük risk', cls: 'low', name: 'düşük' },
      medium: { text: '🟡 Orta risk', cls: 'medium', name: 'orta' },
      high: { text: '🔴 Yüksek risk', cls: 'high', name: 'yüksek' }
    };

    /* option.risk -> { level, text, cls, reasons, connections, tooltip }, or null when the level is missing or unknown */
    function riskBadgeInfo(risk) {
      if (!isPlainObject(risk)) return null;
      const level = String(risk.level || '').trim().toLowerCase();
      if (!Object.prototype.hasOwnProperty.call(TRIP_RISK_LEVELS, level)) return null;
      const info = TRIP_RISK_LEVELS[level];
      const reasons = tripListItems(risk.reasons);
      const connections = tripListItems(risk.connections);
      const tip = reasons.slice();
      if (connections.length) tip.push(`Aktarmalar: ${connections.join(', ')}`);
      return {
        level, text: info.text, cls: info.cls, reasons, connections,
        tooltip: tip.length ? tip.join(' · ') : `Risk: ${info.name}`
      };
    }

    const TRIP_ADVICE = {
      al: { label: 'Şimdi al', cls: 'al' },
      bekle: { label: 'Bekle', cls: 'bekle' },
      belirsiz: { label: 'Belirsiz', cls: 'belirsiz' }
    };

    /* advice.advice -> { key, label, cls } or null */
    function adviceInfo(advice) {
      if (!isPlainObject(advice)) return null;
      const key = String(advice.advice || '').trim().toLowerCase();
      if (!Object.prototype.hasOwnProperty.call(TRIP_ADVICE, key)) return null;
      return { key, label: TRIP_ADVICE[key].label, cls: TRIP_ADVICE[key].cls };
    }

    /* Confidence as 0-1 fraction or 0-100 percent -> "%72"; '' when unusable */
    function formatConfidence(c) {
      if (!hasNumber(c)) return '';
      const pct = c <= 1 ? Math.round(c * 100) : Math.round(c);
      return pct >= 0 && pct <= 100 ? `%${pct}` : '';
    }

    /* Sorted ISO dates -> { start, end, contiguous }. end is '' for a single date. */
    function isoListToRange(list) {
      const dates = (Array.isArray(list) ? list : []).map(d => String(d)).sort();
      const contiguous = dates.every((d, i) => i === 0 || addDaysIso(dates[i - 1], 1) === d);
      return { start: dates[0] || '', end: contiguous && dates.length > 1 ? dates[dates.length - 1] : '', contiguous };
    }

    function formatTry(n) {
      return hasNumber(n) ? `${Math.round(n).toLocaleString('tr-TR')} ₺` : '--';
    }

    /* Real-only switch: read from storage (try/catch), shown on the button, saved on every change */
    function loadTripRealOnly() {
      try {
        return localStorage.getItem(TRIP_REAL_ONLY_KEY) === '1';
      } catch (e) {
        return false; // storage unavailable: the switch starts off for this page session
      }
    }

    function saveTripRealOnly(on) {
      try {
        localStorage.setItem(TRIP_REAL_ONLY_KEY, on ? '1' : '0');
      } catch (e) {
        // Storage unavailable: the switch still works for this page session.
      }
    }

    function paintTripRealOnly() {
      const btn = document.getElementById('tpRealOnly');
      if (!btn) return;
      btn.setAttribute('aria-pressed', tripRealOnly ? 'true' : 'false');
      btn.classList.toggle('active', tripRealOnly);
    }

    function setTripRealOnly(on) {
      tripRealOnly = !!on;
      saveTripRealOnly(tripRealOnly);
      paintTripRealOnly();
    }

    function toggleTripRealOnly() {
      setTripRealOnly(!tripRealOnly);
    }

    /* Option total for the price box: "1.200 ₺", or "≥ 1.200 ₺" when a price is unknown (a lower bound) */
    function tripTotalLabel(opt) {
      const price = tripPriceOf(opt);
      return opt && opt.price_complete === false && hasNumber(price) ? `≥ ${formatTry(price)}` : formatTry(price);
    }

    /* "(+2 bacak fiyatı bilinmiyor · transfer fiyatı yok)" for an incomplete option; '' when every price is known */
    function tripUnknownNote(opt) {
      if (!isPlainObject(opt) || opt.price_complete !== false) return '';
      const bits = [];
      const legs = hasNumber(opt.unknown_price_legs) ? opt.unknown_price_legs : 0;
      if (legs > 0) bits.push(`+${Math.round(legs)} bacak fiyatı bilinmiyor`);
      tripListItems(opt.unknown_price_parts).forEach(p => {
        if (Object.prototype.hasOwnProperty.call(TRIP_UNKNOWN_PART_LABELS, p)) bits.push(`${TRIP_UNKNOWN_PART_LABELS[p]} fiyatı yok`);
      });
      return bits.length ? `(${bits.join(' · ')})` : '';
    }

    /* Verdict against a budget. An incomplete total is a lower bound: it can prove "above budget", never "below". */
    function tripBudgetVerdictHtml(opt, price, budget) {
      const overBudget = hasNumber(price) && hasNumber(budget) && budget > 0 && price > budget;
      if (isPlainObject(opt) && opt.price_complete === false && hasNumber(price) && !overBudget) {
        return '<span class="tp-verdict neutral">Fiyat eksik: bütçe kontrolü yapılamadı</span>';
      }
      return budgetVerdictHtml(price, budget);
    }

    /* Option kind -> Turkish label */
    function tripKindLabel(opt) {
      const kind = opt ? opt.kind : '';
      const via = opt && opt.via_stop ? String(opt.via_stop) : '';
      if (kind === 'direct') return 'Direkt';
      if (kind === 'side_route') return 'Yan yol';
      if (kind === 'via_single_ticket') return via ? `Tek bilet – ${via} aktarmalı` : 'Tek bilet – aktarmalı';
      if (kind === 'via_separate') return via ? `Ayrı biletler – ${via} üzerinden` : 'Ayrı biletler – aktarmalı';
      if (kind === 'ground') return 'Sadece tren/otobüs';
      if (kind === 'multi_flight') return 'Ayrı biletli uçuşlar';
      return kind ? String(kind) : 'Seçenek';
    }

    /* Guaranteed-true total when present, otherwise the plain total */
    function tripPriceOf(opt) {
      if (hasNumber(opt.true_total_try)) return opt.true_total_try;
      if (hasNumber(opt.total_try)) return opt.total_try;
      return null;
    }

    /* Total vs budget: "✅ bütçenin altında" or "❌ bütçenin X ₺ üstünde" */
    function budgetVerdictHtml(total, budget) {
      if (!hasNumber(total)) return '';
      if (!hasNumber(budget) || budget <= 0) return '<span class="tp-verdict neutral">Bütçe girilmedi</span>';
      if (total <= budget) return '<span class="tp-verdict ok">✅ bütçenin altında</span>';
      return `<span class="tp-verdict bad">❌ bütçenin ${Math.round(total - budget).toLocaleString('tr-TR')} ₺ üstünde</span>`;
    }

    function tripLegIcon(leg, type) {
      const vt = Array.isArray(leg && leg.vehicle_types) ? leg.vehicle_types.map(x => String(x).toLowerCase()) : [];
      if (vt.includes('bus') && vt.includes('train')) return '🚌+🚆';
      if (type === 'flight') return '✈️';
      if (type === 'train') return '🚆';
      if (type === 'bus') return '🚌';
      if (type === 'transfer') return /metro/i.test(String((leg && leg.operator) || '')) ? '🚇' : '🔄';
      return '•';
    }

    // Source tag for ground legs. Empty unless the leg carries a `source` (segments from the ground
    // providers do; the route engine's legs do not yet, so no tag is shown for them).
    const GROUND_SOURCE_TAGS = new Map([
      ['live', ['RegioJet', 'tp-source']],
      ['regiojet', ['RegioJet', 'tp-source']],
      ['flixbus', ['FlixBus', 'tp-source']],
      ['table', ['tablo (tahmini)', 'tp-flag']],
      ['estimate', ['tahmin', 'tp-flag']],
    ]);
    function groundSourceTag(leg) {
      const entry = GROUND_SOURCE_TAGS.get(String((leg && leg.source) || '').toLowerCase());
      return entry ? ` <span class="${entry[1]}">${escapeHtml(entry[0])}</span>` : '';
    }

    // One line for the header tooltip: a ground source's /api/health entry in plain Turkish
    function groundStatusText(h) {
      if (!h || typeof h !== 'object') return 'bilinmiyor';
      if (h.available === false) return 'kapalı';
      if (h.status === 'ok') return 'çalışıyor';
      if (h.status === 'blocked') return 'sorunlu (engellendi)';
      if (h.status === 'error') return h.last_error ? `sorunlu (${h.last_error})` : 'sorunlu';
      if (h.status === 'unknown') return 'henüz istek yapılmadı';
      return 'bilinmiyor';
    }

    /* Transfer mode (operator of a transfer leg) -> Turkish label; unknown modes never show the raw text */
    const TRIP_TRANSFER_MODES = {
      metro: 'metro', subway: 'metro',
      taxi: 'taksi', taksi: 'taksi',
      bus: 'otobüs', otobus: 'otobüs', 'otobüs': 'otobüs',
      train: 'tren', tren: 'tren',
      servis: 'servis', shuttle: 'servis',
      estimated: 'Tahmini transfer', tahmini: 'Tahmini transfer'
    };

    function tripTransferLabel(leg) {
      const key = String(leg.operator || '').trim().toLowerCase();
      if (Object.prototype.hasOwnProperty.call(TRIP_TRANSFER_MODES, key)) return TRIP_TRANSFER_MODES[key];
      return leg.is_estimated ? 'Tahmini transfer' : 'transfer';
    }

    /* Flight stop tag text. Number/list -> "aktarmalı" when there are stops; the backend's string wording
       ("Aktarmasız", "N aktarma", "Aktarmalı (tahmini)", "bilinmiyor") is mapped to Turkish labels. '' = no tag. */
    function tripLegStopLabel(leg) {
      const s = leg.stops;
      if (hasNumber(s)) return s > 0 ? 'aktarmalı' : '';
      if (Array.isArray(s)) return tripListItems(s).length > 0 ? 'aktarmalı' : '';
      if (typeof s !== 'string') return '';
      const low = s.trim().toLowerCase();
      if (low === 'aktarmasız' || low === 'nonstop') return '';
      if (low === 'bilinmiyor') return 'aktarma bilgisi yok';
      if (low.startsWith('aktarmalı')) return low.indexOf('tahmini') >= 0 ? 'aktarmalı (tahmini)' : 'aktarmalı';
      const m = /^(\d+)\s*aktarma$/.exec(low);
      if (m) return Number(m[1]) > 0 ? `${m[1]} aktarma` : '';
      return '';
    }

    function locativeSuffix(name) {
      const s = String(name || '').trim();
      if (!s) return "'de";
      const vowels = s.toLowerCase().match(/[aıoueiöü]/g);
      const lastVowel = vowels ? vowels[vowels.length - 1] : 'e';
      const lastChar = s.slice(-1).toLowerCase();
      const dOrT = 'fstkçşhp'.includes(lastChar) ? 't' : 'd';
      const aOrE = 'aıou'.includes(lastVowel) ? 'a' : 'e';
      return `'${dOrT}${aOrE}`;
    }

    function isCityName(place) {
      if (!place || typeof place !== 'string') return false;
      const p = place.trim();
      if (p.length === 3 && p === p.toUpperCase() && /^[A-Z]{3}$/.test(p)) return false;
      if (p.endsWith(' Airport') || p.endsWith(' Havalimanı')) return false;
      return true;
    }

    function parseIsoMins(val) {
      if (!val) return null;
      const d = new Date(val);
      return isNaN(d.getTime()) ? null : d.getTime() / 60000;
    }

    function cityLayoverNote(legA, legB, stopoverPlace) {
      if (!legA || !legB) return null;
      const city = legA.to;
      if (!isCityName(city)) return null;
      if (stopoverPlace && String(stopoverPlace).trim().toLowerCase() === String(city).trim().toLowerCase()) {
        return null;
      }
      const arr = parseIsoMins(legA.arrive_utc || legA.arrive_at);
      const dep = parseIsoMins(legB.depart_utc || legB.depart_at);
      if (arr === null || dep === null || dep <= arr) return null;
      const waitMins = Math.round(dep - arr);
      if (waitMins < 180) return null;
      const h = Math.floor(waitMins / 60);
      const m = waitMins % 60;
      const timeStr = m === 0 ? `${h} saat` : `${h} saat ${m} dk`;
      return `${city}${locativeSuffix(city)} ${timeStr} serbest`;
    }

    function renderTripLeg(leg) {
      const type = String(leg.type || '');
      const icon = tripLegIcon(leg, type);
      const nodeClass = ['flight', 'train', 'bus', 'transfer'].indexOf(type) >= 0 ? type : '';
      // Ground legs carry the exact station name; prefer it over the city name
      const fromName = leg.origin_station_name || leg.from;
      const toName = leg.dest_station_name || leg.to;
      let price = '';
      if (hasNumber(leg.price_try) && leg.price_try > 0) {
        price = ` · <strong style="color: var(--accent);">${escapeHtml(formatTry(leg.price_try))}</strong>`;
      } else if (leg.price_try === null || leg.price_try === undefined) {
        // Unknown price: never a number. An indicative "from" price is shown as such when the source has one.
        const from = hasNumber(leg.price_from_try) && leg.price_from_try > 0
          ? `${formatTry(leg.price_from_try)}'den başlayan` : 'fiyat yok';
        price = ` · <strong style="color: var(--text-muted);">${escapeHtml(from)}</strong>`;
      }
      const est = leg.is_estimated ? ' <span class="tp-flag">⚠️ Tahmini tarife</span>' : '';
      const srcTag = (type === 'train' || type === 'bus') ? groundSourceTag(leg) : '';
      const stopLabel = type === 'flight' ? tripLegStopLabel(leg) : '';
      const stopTag = stopLabel ? ` <span class="tp-badge">${escapeHtml(stopLabel)}</span>` : '';
      const lastTag = leg.is_last_service ? ` <span class="tp-flag">${escapeHtml('Günün son seferi')}</span>` : '';
      let nextTag = '';
      if (leg.next_departure_at) {
        const hm = splitTripDateTime(leg.next_departure_at).time || String(leg.next_departure_at).slice(-5);
        if (hm) {
          nextTag = ` <span class="tp-badge">${escapeHtml(`Sonraki sefer: ${hm}`)}</span>`;
        }
      }
      const freshnessTag = (type === 'flight' && leg.observed_at)
        ? ` <span class="tp-badge" title="${escapeHtml(leg.observed_at)}">🕒 ${escapeHtml(formatObservedAt(leg.observed_at))}</span>`
        : '';
      const vt = Array.isArray(leg && leg.vehicle_types) ? leg.vehicle_types.map(x => String(x).toLowerCase()) : [];
      const isMixed = vt.includes('bus') && vt.includes('train');
      let legName = type === 'transfer' ? tripTransferLabel(leg) : (leg.operator || type || 'Ulaşım');
      if (isMixed) {
        legName = leg.operator ? `${leg.operator} (otobüs + tren)` : 'otobüs + tren';
      }
      const href = safeUrl(leg.booking_url);
      const link = href !== '#'
        ? `<a class="tp-book" href="${href}" target="_blank" rel="noopener noreferrer">🎫 Bilet Al ↗</a>` : '';
      return `
        <div class="timeline-node ${nodeClass}">
          <div class="tp-leg-row">
            <div>
              <span>${icon} <strong>${escapeHtml(legName)}</strong>${stopTag} (${escapeHtml(fromName)} ➔ ${escapeHtml(toName)})</span>
              <div class="tp-leg-time">⏰ ${escapeHtml(formatTrDateTimeOrRaw(leg.depart_at))} → ${escapeHtml(formatTrDateTimeOrRaw(leg.arrive_at))}${price}${est}${srcTag}${lastTag}${nextTag}${freshnessTag}</div>
            </div>
            ${link}
          </div>
        </div>`;
    }

    /* opts: { label, highlight, showBudget, budget } */
    function renderTripOptionCard(opt, opts) {
      const o = opts || {};
      const price = tripPriceOf(opt);
      const flags = [];
      if (opt.night_wait) flags.push('<span class="tp-flag">🌙 Gece beklemesi</span>');
      if (opt.separate_tickets) flags.push('<span class="tp-flag">⚠️ Ayrı bilet riski</span>');
      if (opt.has_estimated_segments) flags.push('<span class="tp-flag">⚠️ Tahmini tarife</span>');
      const risk = riskBadgeInfo(opt.risk);
      if (risk) flags.unshift(`<span class="tp-risk ${risk.cls}" title="${escapeHtml(risk.tooltip)}">${escapeHtml(risk.text)}</span>`);
      const badges = (Array.isArray(opt.badges) ? opt.badges : [])
        .map(b => `<span class="tp-badge">🏷️ ${escapeHtml(b)}</span>`).join(' ');
      const stops = tripListItems(opt.stops);
      const stopsHtml = stops.length ? `<span>🔀 Duraklar: ${escapeHtml(stops.join(' ➔ '))}</span>` : '';
      const stopover = isPlainObject(opt.stopover) && opt.stopover.place
        ? `<div class="tp-stopover">⏸️ Mola: ${escapeHtml(opt.stopover.place)}${hasNumber(opt.stopover.mins) ? ` · ${escapeHtml(formatSaDk(opt.stopover.mins))}` : ''}</div>`
        : '';
      const riskDetails = risk && (risk.reasons.length || risk.connections.length)
        ? `<details class="tp-reasons"><summary>Risk nedenleri (${risk.reasons.length})</summary>`
          + `<ul>${risk.reasons.map(r => `<li>${escapeHtml(r)}</li>`).join('')}</ul>`
          + (risk.connections.length ? `<div>Aktarmalar: ${escapeHtml(risk.connections.join(', '))}</div>` : '')
          + '</details>'
        : '';
      const costText = formatCostBreakdownText(opt.cost_breakdown);
      const costHtml = costText ? `<div class="tp-cost">💳 ${escapeHtml(costText)}</div>` : '';
      const stopoverPlace = isPlainObject(opt.stopover) && opt.stopover.place ? opt.stopover.place : null;
      const legs = (Array.isArray(opt.legs) ? opt.legs : []).filter(l => l && typeof l === 'object');
      const legsHtml = legs.length ? `<div class="timeline-container">${legs.map((leg, idx) => {
        let nodeHtml = renderTripLeg(leg);
        if (idx < legs.length - 1) {
          const note = cityLayoverNote(leg, legs[idx + 1], stopoverPlace);
          if (note) {
            nodeHtml += `<div class="tp-city-layover" style="margin: 4px 0 8px 32px; padding: 4px 10px; background: rgba(59, 130, 246, 0.08); border-left: 3px solid var(--accent, #3b82f6); border-radius: 4px; font-size: 13px; font-weight: 600; color: var(--accent, #3b82f6);">🏙️ ${escapeHtml(note)}</div>`;
          }
        }
        return nodeHtml;
      }).join('')}</div>` : '';
      const dur = hasNumber(opt.duration_mins) ? formatSaDk(opt.duration_mins) : '--';
      const nightsLine = formatNightsLine(opt);
      const nightsHtml = nightsLine ? `<div class="tp-nights" style="margin-top: 4px; font-size: 13px; font-weight: 600; color: var(--text-muted);">${escapeHtml(nightsLine)}</div>` : '';

      return `
        <div class="tp-card${o.highlight ? ' tp-card-highlight' : ''}">
          ${o.label ? `<div class="tp-highlight">${escapeHtml(o.label)}</div>` : ''}
          <div class="tp-card-head">
            <div style="flex: 1; min-width: 0;">
              <div class="tp-kind">${escapeHtml(tripKindLabel(opt))}${flags.join('')}</div>
              <div class="tp-times">${escapeHtml(formatTrDateTimeOrRaw(opt.depart_at))} → ${escapeHtml(formatTrDateTimeOrRaw(opt.arrive_at))}</div>
              <div class="tp-meta"><span>⏱️ ${escapeHtml(dur)}</span>${stopsHtml}${badges}</div>
              ${nightsHtml}
              ${stopover}
              ${riskDetails}
            </div>
            <div class="tp-price-box">
              <div class="tp-price">${escapeHtml(tripTotalLabel(opt))}</div>
              ${tripUnknownNote(opt) ? `<div class="tp-price-note">${escapeHtml(tripUnknownNote(opt))}</div>` : ''}
              ${o.showBudget ? tripBudgetVerdictHtml(opt, price, o.budget) : ''}
            </div>
          </div>
          ${costHtml}
          ${legsHtml}
        </div>`;
    }

    function renderTripBest(best, budget, label, emptyText) {
      if (!best || typeof best !== 'object') {
        return `<div class="tp-best-empty"><div class="tp-highlight">${escapeHtml(label)}</div>${escapeHtml(emptyText)}</div>`;
      }
      return renderTripOptionCard(best, { label, highlight: true, showBudget: true, budget });
    }

    /* Identity of a trip option: departure, arrival, true total and the route legs */
    function tripOptionKey(opt) {
      return JSON.stringify([
        opt.depart_at || '',
        opt.arrive_at || '',
        tripPriceOf(opt),
        Array.isArray(opt.legs) ? opt.legs : []
      ]);
    }

    /* True when both values are trip options describing the same offer (legs, depart_at, true total) */
    function sameTripOption(a, b) {
      if (!isPlainObject(a) || !isPlainObject(b)) return false;
      return tripOptionKey(a) === tripOptionKey(b);
    }

    /* Summary slots of the "best" grid, in display order (Önerilen first) */
    const TRIP_SUMMARY_SLOTS = [
      { key: 'best', text: 'Önerilen', icon: '✨', budget: null },
      { key: 'best_direct', text: 'En iyi direkt', icon: '🛫', budget: 'direct' },
      { key: 'best_side_route', text: 'En iyi yan yol', icon: '🚆', budget: 'side' },
      { key: 'cheapest', text: 'En ucuz', icon: '💸', budget: null },
      { key: 'fastest', text: 'En hızlı', icon: '⚡', budget: null }
    ];

    /* data (best, best_direct, best_side_route, cheapest, fastest) -> one group per distinct option:
       [{ opt, label, budget }]. Slots holding the same offer (sameTripOption) share one group;
       budget is 'direct' or 'side' when a direct/side slot is in the group, else null. Nulls are ignored. */
    function groupTripSummaries(data) {
      const groups = [];
      const src = isPlainObject(data) ? data : {};
      TRIP_SUMMARY_SLOTS.forEach(slot => {
        const opt = src[slot.key];
        if (!isPlainObject(opt)) return;
        let g = groups.find(x => sameTripOption(x.opt, opt));
        if (!g) {
          g = { opt, slots: [], budget: null };
          groups.push(g);
        }
        g.slots.push(slot);
        if (slot.budget === 'direct' || (slot.budget === 'side' && g.budget !== 'direct')) g.budget = slot.budget;
      });
      return groups.map(g => ({
        opt: g.opt,
        label: `${g.slots[0].icon} ${g.slots.map(s => s.text).join(' · ')}`,
        budget: g.budget
      }));
    }

    /* Search hit the flight-search budget: offer a re-run with double budget (max 60) */
    function renderBudgetBanner(f) {
      const current = parseIntInRange(f.flightBudget, 1, TRIP_BUDGET_MAX, TRIP_BUDGET_DEFAULT);
      const next = Math.min(TRIP_BUDGET_MAX, current * 2);
      const action = current >= TRIP_BUDGET_MAX
        ? `<button type="button" class="filter-btn" disabled>Bütçe üst sınırda (${TRIP_BUDGET_MAX})</button>`
        : `<button type="button" class="filter-btn active" onclick="runTripSearchWithBudget(${next})">🔎 Derin arama (bütçe ×2)</button>`;
      return `<div class="tp-banner"><span>Arama bütçesi doldu — daha fazla aday var</span>${action}</div>`;
    }

    /* data.candidates -> "Denenen kalkış: …, varış: …, aktarma merkezleri: …" */
    function renderCandidatesSummary(c) {
      if (!isPlainObject(c)) return '';
      const origin = tripListItems(c.origin_side);
      const dest = tripListItems(c.dest_side);
      const hubs = tripListItems(c.hubs);
      if (!origin.length && !dest.length && !hubs.length) return '';
      return `<div class="tp-candidates">🔍 Denenen kalkış: ${escapeHtml(origin.join(', ') || '--')} · varış: ${escapeHtml(dest.join(', ') || '--')} · aktarma merkezleri: ${escapeHtml(hubs.join(', ') || '--')}</div>`;
    }

    /* data.advice -> collapsible pill. Always framed as a heuristic based on past searches. */
    function renderTripAdvice(advice) {
      const info = adviceInfo(advice);
      if (!info) return '';
      const reasons = tripListItems(advice.reasons);
      const stats = isPlainObject(advice.stats)
        ? Object.keys(advice.stats)
          .filter(k => ['number', 'string', 'boolean'].indexOf(typeof advice.stats[k]) >= 0)
          .map(k => `${k}: ${advice.stats[k]}`)
        : [];
      const conf = formatConfidence(advice.confidence);
      const sub = ['geçmiş aramalara göre', conf ? `güven ${conf}` : ''].filter(Boolean).join(' · ');
      return `
        <details class="tp-advice ${info.cls}">
          <summary>💡 Öneri: ${escapeHtml(info.label)} <span class="tp-advice-sub">(${escapeHtml(sub)})</span></summary>
          <div class="tp-advice-body">
            ${reasons.length ? `<ul>${reasons.map(r => `<li>${escapeHtml(r)}</li>`).join('')}</ul>` : '<div>Ek neden bilgisi yok.</div>'}
            ${stats.length ? `<div>İstatistikler: ${escapeHtml(stats.join(' · '))}</div>` : ''}
            <div style="margin-top: 4px;">Bu sezgisel bir öneridir; fiyatın gerçek seyrini garanti etmez.</div>
          </div>
        </details>`;
    }

    /* data.flex -> date strip with the cheapest price per date, best date and saving */
    function renderFlexStrip(flex) {
      if (!isPlainObject(flex) || !Array.isArray(flex.by_date) || !flex.by_date.length) return '';
      const cells = flex.by_date.filter(isPlainObject).map(x => {
        const isBest = x.date === flex.best_date;
        const isReq = x.date === flex.requested_date;
        const classes = ['tp-flex-cell', isBest ? 'best' : '', isReq ? 'req' : ''].filter(Boolean).join(' ');
        const dateText = formatTrShortDate(x.date, 0) || String(x.date || '--');
        return `
          <div class="${classes}">
            <div class="tp-flex-date">${escapeHtml(dateText)}</div>
            <div class="tp-flex-price">${escapeHtml(formatTry(x.best_true_total_try))}</div>
            ${isBest ? '<div class="tp-flex-tag">en ucuz</div>' : ''}
            ${isReq ? '<div class="tp-flex-tag">talep</div>' : ''}
          </div>`;
      }).join('');
      const parts = [];
      if (flex.best_date && hasNumber(flex.best_price)) {
        parts.push(`🎯 En ucuz tarih: ${formatTrShortDate(flex.best_date, 0) || flex.best_date} (${formatTry(flex.best_price)})`);
      }
      if (hasNumber(flex.saving_vs_requested) && flex.saving_vs_requested > 0) {
        parts.push(`💰 Talep ettiğiniz tarihe göre ${formatTry(flex.saving_vs_requested)} tasarruf`);
      }
      return `
        <div class="tp-flex">
          <div class="tp-section-title">Esnek tarihler</div>
          <div class="tp-flex-strip">${cells}</div>
          ${parts.length ? `<div class="tp-flex-summary">${escapeHtml(parts.join(' · '))}</div>` : ''}
        </div>`;
    }

    /* A combo refers to an option either inline or by index into the list */
    function resolveTripOption(ref, list) {
      if (isPlainObject(ref)) return ref;
      const arr = Array.isArray(list) ? list : [];
      const idx = typeof ref === 'number' ? ref : (typeof ref === 'string' && /^\d+$/.test(ref) ? Number(ref) : -1);
      return idx >= 0 && idx < arr.length && isPlainObject(arr[idx]) ? arr[idx] : null;
    }

    /* data.roundtrip.combos -> expandable list; opening a combo shows both its options */
    function renderTripCombos(data) {
      const rt = data.roundtrip;
      if (!isPlainObject(rt)) return '';
      const combos = (Array.isArray(rt.combos) ? rt.combos : []).filter(isPlainObject);
      if (!combos.length) {
        return '<div class="tp-section-title">Gidiş-dönüş kombinasyonları</div><div class="tp-best-empty">Bu kurallarla gidiş-dönüş kombinasyonu bulunamadı.</div>';
      }
      const retOptions = isPlainObject(rt.return) && Array.isArray(rt.return.options) ? rt.return.options : [];
      const rows = combos.map(c => {
        const out = resolveTripOption(c.outbound, data.options);
        const ret = resolveTripOption(c.return, retOptions);
        const label = tripListText(c.label) || 'Gidiş-dönüş';
        const stay = hasNumber(c.stay_hours) ? formatSaDk(Math.round(c.stay_hours * 60)) : '--';
        const badges = tripListItems(c.badges).map(b => `<span class="tp-badge">🏷️ ${escapeHtml(b)}</span>`).join(' ');
        return `
          <details class="tp-combo">
            <summary>
              <div class="tp-combo-head">
                <span class="tp-kind">${escapeHtml(label)}</span>
                <span class="tp-price">${escapeHtml(formatTry(c.total_try))}</span>
              </div>
              <div class="tp-meta"><span>🏨 Hedefte: ${escapeHtml(stay)}</span>${badges}</div>
            </summary>
            <div class="tp-combo-body">
              ${out ? renderTripOptionCard(out, { label: 'Gidiş' }) : '<div class="tp-best-empty">Gidiş seçeneği bulunamadı.</div>'}
              ${ret ? renderTripOptionCard(ret, { label: 'Dönüş' }) : '<div class="tp-best-empty">Dönüş seçeneği bulunamadı.</div>'}
            </div>
          </details>`;
      }).join('');
      return `<div class="tp-section-title">Gidiş-dönüş kombinasyonları (${combos.length})</div>${rows}`;
    }

    function renderTripResults(data, f) {
      const el = document.getElementById('tripResults');
      if (!el) return;
      const options = (Array.isArray(data.options) ? data.options : []).filter(o => o && typeof o === 'object');
      const rt = isPlainObject(data.roundtrip) ? data.roundtrip : null;
      const warnings = [].concat(
        Array.isArray(data.warnings) ? data.warnings : [],
        rt && isPlainObject(rt.return) && Array.isArray(rt.return.warnings) ? rt.return.warnings : []
      );
      // Real-only search with nothing left: say so, and how to see the estimated routes (unless the server already did)
      if (f.realOnly && !options.length && !warnings.some(w => String(w).indexOf('Gerçek veriyle seçenek bulunamadı') === 0)) {
        warnings.unshift(TRIP_REAL_ONLY_EMPTY_TEXT);
      }
      const searched = Array.isArray(data.searched_dates) ? data.searched_dates : [];
      const directBudget = parseTripBudget(f.directBudget);
      const sideBudget = parseTripBudget(f.sideBudget);

      const warnItems = warnings.map(w => `<div>⚠️ ${escapeHtml(w)}</div>`).join('');
      // With options on screen, warnings collapse into a details block; without options they stay visible
      const warnHtml = !warnings.length ? ''
        : options.length
          ? `<details class="tp-warn-details"><summary>Uyarılar (${warnings.length})</summary><div class="tp-warnings">${warnItems}</div></details>`
          : `<div class="tp-warnings">${warnItems}</div>`;
      const budgetHtml = data.budget_exhausted ? renderBudgetBanner(f) : '';

      const statsParts = [];
      if (searched.length) statsParts.push(`📅 ${searched.map(d => formatTrShortDate(d, 0) || String(d)).join(', ')}`);
      if (hasNumber(data.flight_searches)) statsParts.push(`✈️ ${data.flight_searches} uçuş araması`);
      statsParts.push(`${options.length} seçenek`);

      // One card per distinct option; each card lists every label it answers to
      const summaryCards = groupTripSummaries(data).map(g => renderTripOptionCard(g.opt, {
        label: g.label,
        highlight: true,
        showBudget: g.budget !== null,
        budget: g.budget === 'direct' ? directBudget : (g.budget === 'side' ? sideBudget : null)
      }));
      // Empty slots keep their placeholder so the user sees that no direct / side-route option exists
      const placeholders = [
        isPlainObject(data.best_direct) ? '' : renderTripBest(null, null, '🛫 En iyi direkt', 'Bu kurallarla direkt seçenek bulunamadı.'),
        isPlainObject(data.best_side_route) ? '' : renderTripBest(null, null, '🚆 En iyi yan yol',
          f.sideRoutes ? 'Bu kurallarla yan yol seçeneği bulunamadı.' : 'Yan yollar aramaya dahil edilmedi.')
      ];

      const bestHtml = `
        <div class="tp-best-grid">
          ${summaryCards.join('')}
          ${placeholders.join('')}
        </div>`;

      const listHtml = options.length
        ? `<div class="tp-section-title">Tüm seçenekler (${options.length})</div>${options.map(o => renderTripOptionCard(o, {})).join('')}`
        : '<div class="tp-best-empty">Bu kurallara uyan seçenek bulunamadı.</div>';

      let nearMissHtml = '';
      const nearMisses = (Array.isArray(data.near_misses) ? data.near_misses : []).filter(o => o && typeof o === 'object');
      if (nearMisses.length) {
        const completeOptions = options.filter(o => o.price_complete !== false && hasNumber(tripPriceOf(o)));
        const cheapestComplete = completeOptions.length
          ? Math.min(...completeOptions.map(o => tripPriceOf(o)))
          : null;

        const nmCards = nearMisses.map(nm => {
          const nmPrice = tripPriceOf(nm);
          const diff = (cheapestComplete !== null && hasNumber(nmPrice))
            ? (cheapestComplete - nmPrice)
            : null;
          const lateMins = Number(nm.late_by_mins) || 0;
          const lateH = Math.floor(lateMins / 60);
          const lateM = lateMins % 60;
          const lateByStr = lateM ? `${lateH} saat ${lateM} dk` : `${lateH} saat`;

          let label = `${lateByStr} geç varıyor`;
          if (diff !== null && diff > 0) {
            label += ` · ${Math.round(diff).toLocaleString('tr-TR')} TL daha ucuz`;
          }
          return renderTripOptionCard(nm, { label, highlight: false });
        });

        nearMissHtml = `
          <div class="tp-section-title" style="margin-top: 24px;">Kurala kıl payı uymayanlar (${nearMisses.length})</div>
          ${nmCards.join('')}`;
      }

      el.innerHTML = `
        <div class="tp-results">
          ${budgetHtml}
          ${warnHtml}
          <div class="tp-stats">${escapeHtml(statsParts.join(' · '))}</div>
          ${renderCandidatesSummary(data.candidates)}
          ${renderTripAdvice(data.advice)}
          ${renderFlexStrip(data.flex)}
          ${bestHtml}
          ${listHtml}
          ${nearMissHtml}
          ${renderTripCombos(data)}
        </div>`;
    }

    function showTripMsg(text, kind) {
      const el = document.getElementById('tripFormMsg');
      if (!el) return;
      if (!text) {
        el.style.display = 'none';
        el.innerText = '';
        return;
      }
      const ok = kind === 'ok';
      el.style.display = 'block';
      el.style.background = ok ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)';
      el.style.color = ok ? '#10b981' : '#f87171';
      el.innerText = text;
    }

    function setTripBusy(busy) {
      ['btnTripSearch', 'btnTripWatch'].forEach(id => {
        const b = document.getElementById(id);
        if (b) {
          b.disabled = busy;
          b.style.opacity = busy ? '0.6' : '1';
        }
      });
    }

    /* Error response -> Turkish message from the JSON body ({"message": "..."}), else the status text */
    async function readApiErrorMessage(res, fallback) {
      let text = fallback || ('Sunucu hatası: ' + res.status);
      try {
        const body = await res.json();
        if (body && typeof body.message === 'string' && body.message.trim()) {
          text = body.message.trim();
        }
      } catch (e) {
        // Non-JSON error body: keep the fallback text.
      }
      return text;
    }

    /* Form values -> validated trip rules (shared by search and watch) */
    function buildTripRules(f) {
      if (!f.origin || !f.destination) {
        return { error: 'Lütfen nereden ve nereye alanlarını doldurun.', rules: null };
      }
      const range = expandTripDates(f.dateStart, f.dateEnd);
      if (range.error) return { error: range.error, rules: null };

      // Return leg: active only when a return start date is given
      let returnDates = null;
      let returnArriveBy = null;
      let returnVia = null;
      let minStayHours = null;
      if (f.returnDateStart) {
        const rr = expandTripDates(f.returnDateStart, f.returnDateEnd);
        if (rr.error) return { error: `Dönüş: ${rr.error}`, rules: null };
        returnDates = rr.dates;
        returnArriveBy = combineTripDateTime(f.returnArriveDate, f.returnArriveTime, '23:59') || null;
        returnVia = parseViaList(f.returnVia).join(',') || null;
        minStayHours = parseNonNegativeNumber(f.minStayHours);
      } else if (f.returnArriveDate || f.returnVia || f.minStayHours) {
        return { error: 'Dönüş kuralları için önce dönüş kalkış tarihini girin.', rules: null };
      }

      const minStayItems = parseMinStay(f.minStay);
      const bag = bagFlags(f.bag);
      return {
        error: '',
        rules: {
          origin: f.origin,
          destination: f.destination,
          schengen_single_entry: Boolean(f.schengenSingleEntry),
          depart_dates: range.dates,
          arrive_by: combineTripDateTime(f.arriveDate, f.arriveTime, '23:59') || null,
          depart_after: combineTripDateTime(f.afterDate, f.afterTime, '00:00') || null,
          via: parseViaList(f.via).join(',') || null,
          include_side_routes: !!f.sideRoutes,
          max_stops: parseIntInRange(f.maxStops, 0, 3, 2),
          flight_budget: parseIntInRange(f.flightBudget, 1, TRIP_BUDGET_MAX, TRIP_BUDGET_DEFAULT),
          cabin_bag: bag.cabin_bag,
          checked_bag: bag.checked_bag,
          real_data_only: !!f.realOnly,
          flex_days: parseIntInRange(f.flex, 0, 3, 0),
          min_stay: serializeMinStay(minStayItems),
          min_stay_mins: minStayToObject(minStayItems),
          return_dates: returnDates,
          return_arrive_by: returnArriveBy,
          return_via: returnVia,
          min_stay_hours: minStayHours
        }
      };
    }

    /* "Derin arama": same search with a new flight-search budget (the field is updated so the new value is kept) */
    function runTripSearchWithBudget(budget) {
      const el = document.getElementById('tpFlightBudget');
      if (el) el.value = String(parseIntInRange(budget, 1, TRIP_BUDGET_MAX, TRIP_BUDGET_DEFAULT));
      return runTripSearch();
    }

    async function runTripSearch() {
      const f = readTripForm();
      const built = buildTripRules(f);
      if (built.error) {
        showTripMsg(built.error, 'error');
        return;
      }
      const rules = built.rules;
      const params = new URLSearchParams();
      params.set('origin', rules.origin);
      params.set('destination', rules.destination);
      params.set('depart_dates', rules.depart_dates.join(','));
      if (rules.arrive_by) params.set('arrive_by', rules.arrive_by);
      if (rules.depart_after) params.set('depart_after', rules.depart_after);
      if (rules.via) params.set('via', rules.via);
      params.set('include_side_routes', rules.include_side_routes ? '1' : '0');
      params.set('max_stops', String(rules.max_stops));
      params.set('budget', String(rules.flight_budget));
      params.set('cabin_bag', String(rules.cabin_bag));
      params.set('checked_bag', String(rules.checked_bag));
      params.set('real_data_only', rules.real_data_only ? '1' : '0');
      if (rules.schengen_single_entry) params.set('schengen_single_entry', 'true');
      params.set('flex_days', String(rules.flex_days));
      if (rules.min_stay) params.set('min_stay', rules.min_stay);
      if (rules.return_dates) {
        params.set('return_dates', rules.return_dates.join(','));
        if (rules.return_arrive_by) params.set('return_arrive_by', rules.return_arrive_by);
        if (rules.return_via) params.set('return_via', rules.return_via);
        if (rules.min_stay_hours !== null) params.set('min_stay_hours', String(rules.min_stay_hours));
      }
      params.set('time_value_try_per_hour', String(readTimeValueSetting()));

      saveTripForm();
      showTripMsg('', 'error');
      const resultsEl = document.getElementById('tripResults');
      setTripBusy(true);
      resultsEl.innerHTML = `
        <div class="tp-loading">
          <div style="font-size: 32px; margin-bottom: 10px;">⚡</div>
          Aranıyor… birden fazla tarih ve rota tarandığı için 1-3 dk sürebilir
        </div>`;

      try {
        const res = await fetch(`/api/trip-search?${params.toString()}`);
        if (!res.ok) throw new Error(await readApiErrorMessage(res));
        const data = await res.json();
        renderTripResults(data, f);
      } catch (e) {
        console.error('Trip search error:', e);
        resultsEl.innerHTML = `<div class="tp-error">${escapeHtml(e && e.message ? e.message : 'Gezi araması sırasında hata oluştu.')}</div>`;
      } finally {
        setTripBusy(false);
      }
    }

    async function saveTripWatch() {
      const f = readTripForm();
      const built = buildTripRules(f);
      if (built.error) {
        showTripMsg(built.error, 'error');
        return;
      }
      const directBudget = parseTripBudget(f.directBudget);
      const sideBudget = parseTripBudget(f.sideBudget);
      if (f.directBudget && directBudget === null) {
        showTripMsg('Direkt uçuş bütçesi pozitif bir tutar olmalı.', 'error');
        return;
      }
      if (f.sideBudget && sideBudget === null) {
        showTripMsg('Yan yol bütçesi pozitif bir tutar olmalı.', 'error');
        return;
      }
      if (directBudget === null && sideBudget === null) {
        showTripMsg('Takip için en az bir bütçe (direkt veya yan yol) girin.', 'error');
        return;
      }

      const rules = built.rules;
      saveTripForm();
      showTripMsg('', 'error');
      setTripBusy(true);
      try {
        const res = await fetch('/api/watches', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            origin: rules.origin,
            destination: rules.destination,
            departure_date: rules.depart_dates[0],
            return_date: null,
            trip_type: 'oneway',
            departure_time_slot: 'all',
            target_budget_try: sideBudget,
            direct_budget_try: directBudget,
            include_multimodal: rules.include_side_routes,
            trip: {
              depart_dates: rules.depart_dates,
              arrive_by: rules.arrive_by,
              depart_after: rules.depart_after,
              via: rules.via,
              include_side_routes: rules.include_side_routes,
              max_stops: rules.max_stops,
              flight_search_budget: rules.flight_budget,
              min_stay_mins: rules.min_stay_mins,
              cabin_bag: rules.cabin_bag,
              checked_bag: rules.checked_bag,
              real_data_only: rules.real_data_only,
              schengen_single_entry: rules.schengen_single_entry,
              flex_days: rules.flex_days,
              return_dates: rules.return_dates,
              return_arrive_by: rules.return_arrive_by,
              return_via: rules.return_via,
              min_stay_hours: rules.min_stay_hours
            }
          })
        });
        if (!res.ok) throw new Error(await readApiErrorMessage(res));
        showTripMsg('✅ Gezi takibi kuruldu. Arka plan tarayıcısı bu kurallara göre fiyatları izleyecek.', 'ok');
        loadWatchesCount();
        const modal = document.getElementById('watchlistModal');
        if (modal && modal.style.display === 'flex') openWatchesModal();
      } catch (e) {
        showTripMsg(e && e.message ? e.message : 'Takip kaydedilirken hata oluştu.', 'error');
      } finally {
        setTripBusy(false);
      }
    }

    function readTripForm() {
      const f = {};
      Object.keys(TRIP_FIELD_IDS).forEach(key => {
        const el = document.getElementById(TRIP_FIELD_IDS[key]);
        f[key] = el ? String(el.value || '').trim() : '';
      });
      const chk = document.getElementById('tpSideRoutes');
      f.sideRoutes = chk ? chk.checked : true;
      const schengenChk = document.getElementById('tpSchengenSingleEntry');
      f.schengenSingleEntry = schengenChk ? schengenChk.checked : false;
      const bagEl = document.querySelector('input[name="tpBag"]:checked');
      f.bag = bagEl ? bagEl.value : 'small';
      f.realOnly = tripRealOnly;
      // Min-stay rows (hours) are stored as the "Yer:dakika;..." string
      f.minStay = serializeMinStay(readMinStayRows().map(r => ({ place: r.place, mins: parseHoursToMins(r.hours) })));
      return f;
    }

    function writeTripForm(values) {
      const src = values || {};
      Object.keys(TRIP_FIELD_IDS).forEach(key => {
        const el = document.getElementById(TRIP_FIELD_IDS[key]);
        if (!el) return;
        const raw = src[key];
        const v = (raw === null || raw === undefined) ? (TRIP_FIELD_DEFAULTS[key] || '') : raw;
        el.value = String(v);
      });
      const chk = document.getElementById('tpSideRoutes');
      if (chk) chk.checked = src.sideRoutes !== false;
      const schengenChk = document.getElementById('tpSchengenSingleEntry');
      if (schengenChk) schengenChk.checked = Boolean(src.schengenSingleEntry);
      const bag = TRIP_BAG_VALUES.indexOf(src.bag) >= 0 ? src.bag : 'small';
      const bagEl = document.querySelector(`input[name="tpBag"][value="${bag}"]`);
      if (bagEl) bagEl.checked = true;
      writeMinStayRows(parseMinStay(src.minStay));
      const retBlock = document.getElementById('tpReturnBlock');
      if (retBlock && src.returnDateStart) retBlock.open = true;
      updateTripDatesPreview();
    }

    function readMinStayRows() {
      const box = document.getElementById('tpMinStayRows');
      if (!box) return [];
      return Array.from(box.querySelectorAll('.tp-minstay-row')).map(row => {
        const p = row.querySelector('.tp-minstay-place');
        const h = row.querySelector('.tp-minstay-hours');
        return { place: p ? p.value.trim() : '', hours: h ? h.value : '' };
      });
    }

    /* Adds one "Yer + saat" row to the min-stay list. Built with DOM APIs, so typed values are never parsed as HTML. */
    function addMinStayRow(place, hours) {
      const box = document.getElementById('tpMinStayRows');
      if (!box) return;
      const row = document.createElement('div');
      row.className = 'tp-row tp-minstay-row';
      const p = document.createElement('input');
      p.type = 'text';
      p.className = 'tp-minstay-place';
      p.placeholder = 'Yer (örn. Budapeşte)';
      p.setAttribute('aria-label', 'Durak yeri');
      p.value = place || '';
      const h = document.createElement('input');
      h.type = 'number';
      h.min = '0';
      h.step = '0.5';
      h.className = 'tp-minstay-hours';
      h.placeholder = 'saat';
      h.setAttribute('aria-label', 'Kalış saati');
      h.value = hours || '';
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'filter-btn';
      del.textContent = '✕';
      del.setAttribute('aria-label', 'Satırı sil');
      del.addEventListener('click', () => {
        row.remove();
        onTripFormChange();
      });
      row.append(p, h, del);
      box.appendChild(row);
    }

    function writeMinStayRows(items) {
      const box = document.getElementById('tpMinStayRows');
      if (!box) return;
      box.innerHTML = '';
      items.forEach(it => addMinStayRow(it.place, String(Math.round(it.mins * 100 / 60) / 100)));
    }

    function updateTripDatesPreview() {
      const f = readTripForm();
      setDatesPreview('tpDatesPreview', f.dateStart, f.dateEnd);
      setDatesPreview('tpReturnDatesPreview', f.returnDateStart, f.returnDateEnd);
    }

    function setDatesPreview(elId, startIso, endIso) {
      const el = document.getElementById(elId);
      if (!el) return;
      if (!startIso) {
        el.innerText = '';
        return;
      }
      const range = expandTripDates(startIso, endIso);
      if (range.error) {
        el.innerText = range.error;
        el.style.color = '#b45309';
        return;
      }
      el.style.color = '';
      el.innerText = `${range.dates.map(d => formatTrShortDate(d, 0)).join(', ')} (${range.dates.length} tarih)`;
    }

    function saveTripForm() {
      try {
        localStorage.setItem(TRIP_PLANNER_KEY, JSON.stringify(readTripForm()));
      } catch (e) {
        // Storage unavailable (private mode, blocked): the form still works for this page session.
      }
    }

    function onTripFormChange() {
      saveTripForm();
      updateTripDatesPreview();
    }

    function initTripPlanner() {
      tripRealOnly = loadTripRealOnly();
      paintTripRealOnly();
      let saved = null;
      try {
        const raw = localStorage.getItem(TRIP_PLANNER_KEY);
        saved = raw ? JSON.parse(raw) : null;
      } catch (e) {
        saved = null;
      }
      const todayIso = localTodayIso();
      const base = saved && typeof saved === 'object'
        ? keepFutureTripDates(saved, todayIso)
        : { origin: 'İstanbul', arriveTime: '23:59', sideRoutes: true };
      // Gidiş başlangıcı: kayıt yoksa ya da geçmişte kaldıysa bugün + 14
      if (!base.dateStart) base.dateStart = defaultTripDates(todayIso).depDate;
      // Bitiş tarihi başlangıçtan önce olamaz
      if (base.dateEnd && base.dateEnd < base.dateStart) base.dateEnd = '';
      // Dönüş kalkışı geçmişteyse dönüş kuralları da düşer (aksi halde form hata verir)
      if (!base.returnDateStart) {
        base.returnDateEnd = '';
        base.returnArriveDate = '';
        base.returnVia = '';
        base.minStayHours = '';
      }
      writeTripForm(base);
      const section = document.getElementById('tripPlannerSection');
      if (section) {
        section.addEventListener('input', onTripFormChange);
        section.addEventListener('change', onTripFormChange);
      }
    }

    /* Example preset resolved against today: a past example moves forward (see shiftPresetToFuture) */
    function resolvePresetExample(name, todayIso) {
      const p = TRIP_PRESETS[name];
      return p ? shiftPresetToFuture(p, todayIso || localTodayIso()) : null;
    }

    /* "2026-10-28" + "12:00" -> "28.10 12:00" (chip label) */
    function presetDeadlineText(preset) {
      const m = /^\d{4}-(\d{2})-(\d{2})$/.exec(String(preset.arriveDate || ''));
      return m ? `${m[2]}.${m[1]} ${preset.arriveTime || ''}`.trim() : '';
    }

    /* Example chips: the deadline text follows the resolved example dates; the note shows when one was moved */
    function initPresetExamples() {
      const todayIso = localTodayIso();
      const out = resolvePresetExample('outbound', todayIso);
      const inb = resolvePresetExample('inbound', todayIso);
      const dl = document.getElementById('tpPresetOutDeadline');
      if (dl && out) dl.innerText = presetDeadlineText(out.preset);
      const note = document.getElementById('tpPresetNote');
      if (note) note.style.display = ((out && out.shifted) || (inb && inb.shifted)) ? '' : 'none';
    }

    function applyTripPreset(name) {
      const resolved = resolvePresetExample(name, localTodayIso());
      if (!resolved) return;
      writeTripForm(resolved.preset);
      saveTripForm();
      const note = document.getElementById('tpPresetNote');
      if (note) note.style.display = resolved.shifted ? '' : 'none';
      showTripMsg('Gerçek seyahat bilgileri forma yüklendi. "Ara" ile tarayabilir veya takip kurabilirsiniz.', 'ok');
    }

    function scrollToTripPlanner() {
      const el = document.getElementById('tripPlannerSection');
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    /* ---- Gezi Planı takipleri (watches list) ---- */
    function isTripWatch(w) {
      return !!(w && w.trip_query && typeof w.trip_query === 'object');
    }

    /* last_trip_alert (string or option-like object) -> one readable line */
    function describeTripAlert(a) {
      if (!a) return '';
      if (typeof a === 'string') return a;
      if (typeof a !== 'object') return '';
      // Backend shape: {"direct": price, "side": price} (last alerted price per category)
      if (('direct' in a || 'side' in a) && !a.kind) {
        const cats = [];
        if (typeof a.direct === 'number') cats.push(`Direkt ${formatTry(a.direct)}`);
        if (typeof a.side === 'number') cats.push(`Yan yol ${formatTry(a.side)}`);
        return cats.join(' · ');
      }
      const parts = [];
      if (a.kind) parts.push(tripKindLabel(a));
      const price = tripPriceOf(a);
      if (price !== null) parts.push(formatTry(price));
      const when = formatTrDateTime(a.depart_at);
      if (when) parts.push(when);
      return parts.join(' · ');
    }

    function renderTripWatchRules(w, tq) {
      const dates = Array.isArray(tq.depart_dates) ? tq.depart_dates : [];
      const dateText = dates.map(d => formatTrShortDate(d, 0) || String(d)).join(', ');
      const rulesParts = [`📅 Kalkış: ${dateText || '--'}`];
      const arrive = formatTrDateTime(tq.arrive_by);
      if (arrive) rulesParts.push(`⏰ en geç ${arrive}`);
      const after = formatTrDateTime(tq.depart_after);
      if (after) rulesParts.push(`🕒 en erken ${after}`);
      if (hasNumber(tq.max_stops)) {
        rulesParts.push(`🔀 Ara durak: ${tq.max_stops === 0 ? '0 (sadece direkt)' : `en fazla ${tq.max_stops}`}`);
      }
      if (tq.via) rulesParts.push(`📍 Zorunlu duraklar: ${tq.via}`);
      const minStayText = formatMinStayText(tq.min_stay_mins);
      if (minStayText) rulesParts.push(`⏸️ Durakta en az: ${minStayText}`);
      const bagText = tripBagLabel(tq.cabin_bag, tq.checked_bag);
      if (bagText) rulesParts.push(`🧳 Bagaj: ${bagText}`);
      if (tq.real_data_only === true) rulesParts.push('✅ Sadece gerçek veriler');
      if (hasNumber(tq.flex_days) && tq.flex_days > 0) rulesParts.push(`↔️ Esnek ±${tq.flex_days} gün`);
      const retDates = Array.isArray(tq.return_dates) ? tq.return_dates : [];
      if (retDates.length) {
        rulesParts.push(`↩️ Dönüş: ${retDates.map(d => formatTrShortDate(d, 0) || String(d)).join(', ')}`);
        const retArrive = formatTrDateTime(tq.return_arrive_by);
        if (retArrive) rulesParts.push(`⏰ dönüş en geç ${retArrive}`);
        if (tq.return_via) rulesParts.push(`📍 dönüş duraklar: ${tq.return_via}`);
        if (hasNumber(tq.min_stay_hours)) rulesParts.push(`🏨 hedefte en az ${tq.min_stay_hours} sa`);
      }
      const searchBudget = hasNumber(tq.flight_search_budget) ? tq.flight_search_budget : w.flight_search_budget;
      const budgetText = `💰 Direkt bütçe: ${formatTry(w.direct_budget_try)} · Yan yol bütçesi: ${formatTry(w.target_budget_try)}`
        + (hasNumber(searchBudget) ? ` · 🔎 Arama bütçesi: ${searchBudget}` : '');
      return `<div class="tp-watch-rules">${escapeHtml(rulesParts.join(' · '))}<br>${escapeHtml(budgetText)}</div>`;
    }

    function renderTripWatchCard(w) {
      const tq = w.trip_query;
      const alertText = describeTripAlert(w.last_trip_alert);
      const statusHtml = alertText
        ? `<span style="font-size: 11px; font-weight: 800; background: rgba(16, 185, 129, 0.2); color: #10b981; padding: 2px 8px; border-radius: 4px;">🟢 Son uyarı: ${escapeHtml(alertText)}</span>`
        : '<span style="font-size: 11px; font-weight: 700; background: rgba(245, 158, 11, 0.2); color: #fbbf24; padding: 2px 8px; border-radius: 4px;">🟡 Takipte (kurallara uyan fiyat bekleniyor)</span>';
      return `
        <div class="watch-card">
          <div style="flex: 1; min-width: 0;">
            <div style="font-size: 15px; font-weight: 800; color: var(--text);">
              ${escapeHtml(w.origin)} ➔ ${escapeHtml(w.destination)}
              <span style="font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; background: rgba(124, 58, 237, 0.15); color: var(--purple); font-weight: 700;">🧳 Gezi Planı</span>
            </div>
            ${renderTripWatchRules(w, tq)}
            <div style="margin-top: 6px;">${statusHtml}</div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <button class="filter-btn" onclick="loadTripWatchIntoForm(${jsArg(w.id)})">📥 Forma Yükle</button>
            <button class="btn-delete-watch" onclick="deleteWatch(${jsArg(w.id)})">🗑️ Sil</button>
          </div>
        </div>`;
    }

    /* Puts a trip watch's rules back into the planner form */
    function loadTripWatchIntoForm(id) {
      const w = (window._watchList || []).find(x => x && String(x.id) === String(id));
      if (!w || !isTripWatch(w)) return;
      const tq = w.trip_query;
      const out = isoListToRange(tq.depart_dates);
      const arrive = splitTripDateTime(tq.arrive_by);
      const after = splitTripDateTime(tq.depart_after);
      const ret = isoListToRange(tq.return_dates);
      const retArrive = splitTripDateTime(tq.return_arrive_by);
      const searchBudget = hasNumber(tq.flight_search_budget) ? tq.flight_search_budget : w.flight_search_budget;
      const minStayItems = isPlainObject(tq.min_stay_mins)
        ? Object.keys(tq.min_stay_mins).map(p => ({ place: p, mins: tq.min_stay_mins[p] }))
        : [];
      writeTripForm({
        origin: w.origin || '',
        destination: w.destination || '',
        dateStart: out.start,
        dateEnd: out.end,
        arriveDate: arrive.date,
        arriveTime: arrive.time,
        afterDate: after.date,
        afterTime: after.time,
        via: tq.via || '',
        sideRoutes: tq.include_side_routes !== false,
        directBudget: hasNumber(w.direct_budget_try) ? String(w.direct_budget_try) : '',
        sideBudget: hasNumber(w.target_budget_try) ? String(w.target_budget_try) : '',
        maxStops: hasNumber(tq.max_stops) ? String(tq.max_stops) : undefined,
        flightBudget: hasNumber(searchBudget) ? String(searchBudget) : undefined,
        bag: bagFromFlags(tq.cabin_bag, tq.checked_bag),
        flex: hasNumber(tq.flex_days) ? String(tq.flex_days) : undefined,
        minStay: serializeMinStay(minStayItems),
        returnDateStart: ret.start,
        returnDateEnd: ret.end,
        returnArriveDate: retArrive.date,
        returnArriveTime: retArrive.time,
        returnVia: tq.return_via || '',
        minStayHours: hasNumber(tq.min_stay_hours) ? String(tq.min_stay_hours) : ''
      });
      setTripRealOnly(tq.real_data_only === true);  // the watch's own rule, not the remembered switch
      saveTripForm();
      closeWatchesModal();
      scrollToTripPlanner();
      const dateCount = Array.isArray(tq.depart_dates) ? tq.depart_dates.length : 0;
      showTripMsg(out.contiguous || dateCount <= 1
        ? 'Takip kuralları forma yüklendi.'
        : 'Takipte ardışık olmayan tarihler var; forma yalnızca ilk tarih yüklendi.', 'ok');
    }

    // Initialize: check if user had a previous session saved
    /* Hızlı uçuş araması aç/kapa durumu (varsayılan kapalı) */
    const QUICK_FLIGHT_OPEN_KEY = 'travelplan_quick_flight_open';
    function initQuickFlightDetails() {
      const el = document.getElementById('quickFlightDetails');
      if (!el) return;
      let open = false;
      try {
        open = localStorage.getItem(QUICK_FLIGHT_OPEN_KEY) === '1';
      } catch (e) {
        open = false;
      }
      el.open = open;
      el.addEventListener('toggle', () => {
        try {
          localStorage.setItem(QUICK_FLIGHT_OPEN_KEY, el.open ? '1' : '0');
        } catch (e) {
          // Storage unavailable: the toggle still works for this page session.
        }
      });
    }

    /* Classic search dates: today+14 / today+21 unless a saved value is today or later */
    function initClassicDates(savedSession) {
      const todayIso = localTodayIso();
      const defs = defaultTripDates(todayIso);
      const s = savedSession && typeof savedSession === 'object' ? savedSession : {};
      const depDate = futureOrDefaultIso(s.depDate, defs.depDate, todayIso);
      let retDate = futureOrDefaultIso(s.retDate, defs.retDate, todayIso);
      if (retDate < depDate) retDate = depDate; // ISO strings compare correctly
      document.getElementById('depDate').value = depDate;
      document.getElementById('retDate').value = retDate;
    }

    function initApp() {
      initTimeValueSetting();
      initTripPlanner();
      initPresetExamples();
      initQuickFlightDetails();

      // Apply theme (default to clean light mode)
      const savedTheme = localStorage.getItem('travelplan_theme') || 'light';
      setTheme(savedTheme);

      let savedSession = null;
      try {
        localStorage.removeItem('travelplan_last_search');
        localStorage.removeItem('travelplan_last_results');
        const savedSessionStr = localStorage.getItem(STORAGE_KEY);
        savedSession = savedSessionStr ? JSON.parse(savedSessionStr) : null;
      } catch (err) {
        console.error('LocalStorage init error:', err);
      }
      initClassicDates(savedSession);

      try {
        if (savedSession) {
          const session = savedSession;
          if (session && session.destination && Array.isArray(session.flights) && session.flights.length > 0) {
            if (session.origin) document.getElementById('origin').value = session.origin;
            document.getElementById('destination').value = session.destination;
            if (session.tripType) setTripType(session.tripType);

            allFlights = session.flights;
            renderFlightDashboard(session.flights, session.origin, session.destination, session.tripType, session.dateMatrix, session.priceHistory);
            loadWatchesCount();
            loadAlerts();
            return;
          }
        }
      } catch (err) {
        console.error('LocalStorage init error:', err);
      }

      document.getElementById('destination').value = '';
      showEmptyState();
      loadWatchesCount();
      loadAlerts();
    }

    /* ---- Uçuş verisi sağlığı (header göstergesi) ---- */
    async function loadFlightHealth() {
      const pill = document.getElementById('flightHealth');
      const text = document.getElementById('flightHealthText');
      if (!pill || !text) return;
      let state = 'unknown';
      let label = 'Uçuş verisi: bilinmiyor';
      let tooltip = 'Sağlık bilgisi alınamadı';
      try {
        const res = await fetch('/api/health');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const flights = data.flights || {};
        const fx = data.fx || {};
        const rate = (typeof fx.rate === 'number') ? fx.rate.toFixed(2) : '?';
        const source = fx.source ? ` (${fx.source}${fx.is_live ? ', canlı' : ''})` : '';
        // Ground sources (RegioJet, FlixBus) are listed under the exchange rate, one per line
        const ground = data.ground_sources || {};
        const groundLines = [['RegioJet', ground.regiojet], ['FlixBus', ground.flixbus]]
          .filter(([, h]) => h !== undefined)
          .map(([name, h]) => `${name}: ${groundStatusText(h)}`);
        tooltip = [`EUR/TRY: ${rate}${source}`, ...groundLines].join('\n');
        if (flights.last_status) {
          const okStatus = flights.last_status === 'ok' || flights.last_status === 'no_flights';
          if (flights.healthy && okStatus) {
            state = 'ok';
            label = 'Uçuş verisi: çalışıyor';
          } else {
            state = 'bad';
            label = `Uçuş verisi: sorunlu (${flights.last_status})`;
          }
        }
      } catch (e) {
        // keep the grey state; the indicator must never break the page
      }
      pill.className = state === 'unknown' ? 'health-pill' : `health-pill ${state}`;
      text.textContent = label;
      pill.title = tooltip;
    }

    // Keyboard support: Press Enter in origin or destination to search
    ['origin', 'destination'].forEach(id => {
      const el = document.getElementById(id);
      if (el) {
        el.addEventListener('keyup', (e) => {
          if (e.key === 'Enter') searchLiveFlights();
        });
      }
    });


    /* ---- T18: Tur Keşfi Sekmesi ve Tablosu (PLN-15b) ---- */
    function switchAppTab(tab) {
      const isTour = tab === 'tour';
      const plannerSec = document.getElementById('tripPlannerSection');
      const tourSec = document.getElementById('tourExploreSection');
      const btnPlanner = document.getElementById('tabBtnTripPlanner');
      const btnTour = document.getElementById('tabBtnTourExplore');

      if (plannerSec) plannerSec.style.display = isTour ? 'none' : 'block';
      if (tourSec) tourSec.style.display = isTour ? 'block' : 'none';
      if (btnPlanner) btnPlanner.classList.toggle('active', !isTour);
      if (btnTour) btnTour.classList.toggle('active', isTour);
    }
    if (typeof window !== 'undefined') window.switchAppTab = switchAppTab;

    function scrollToTourExplore() {
      const el = document.getElementById('tourExploreSection');
      if (el) el.scrollIntoView({ behavior: 'smooth' });
    }
    if (typeof window !== 'undefined') window.scrollToTourExplore = scrollToTourExplore;

    function showTourMsg(text, kind) {
      const el = document.getElementById('tourFormMsg');
      if (!el) return;
      if (!text) {
        el.textContent = '';
        el.className = 'tp-msg';
        el.style.display = 'none';
        return;
      }
      el.textContent = text;
      el.className = `tp-msg ${kind === 'error' ? 'tp-msg-error' : (kind === 'ok' ? 'tp-msg-ok' : 'tp-msg-info')}`;
      el.style.display = 'block';
    }

    async function runTourExplore() {
      const btnTourSearch = document.getElementById('btnTourSearch');
      const spinner = document.getElementById('tourSearchSpinner');
      const resContainer = document.getElementById('tourResults');
      showTourMsg('', '');

      const origin = (document.getElementById('tourOrigin')?.value || '').trim();
      const destination = (document.getElementById('tourDestination')?.value || '').trim();
      const dateStart = (document.getElementById('tourDateStart')?.value || '').trim();
      const dateEnd = (document.getElementById('tourDateEnd')?.value || '').trim();
      const arriveDate = (document.getElementById('tourArriveDate')?.value || '').trim();
      const arriveTime = (document.getElementById('tourArriveTime')?.value || '23:59').trim();
      const candidates = (document.getElementById('tourCandidates')?.value || '').trim();
      const minStayHours = parseFloat(document.getElementById('tourMinStayHours')?.value || '4');
      const budget = parseInt(document.getElementById('tourBudget')?.value || '8', 10);

      if (!origin || !destination) {
        showTourMsg('Lütfen kalkış ve varış şehirlerini girin.', 'error');
        return;
      }
      if (!dateStart) {
        showTourMsg('Lütfen en az bir kalkış tarihi girin.', 'error');
        return;
      }

      const range = expandTripDates(dateStart, dateEnd);
      if (range.error) {
        showTourMsg(range.error, 'error');
        return;
      }

      const params = new URLSearchParams();
      params.set('origin', origin);
      params.set('destination', destination);
      params.set('depart_dates', range.dates.join(','));

      if (arriveDate) {
        params.set('arrive_by', `${arriveDate}T${arriveTime || '23:59'}`);
      }
      if (candidates) {
        params.set('candidates', candidates);
      }
      if (!isNaN(minStayHours) && minStayHours >= 0) {
        params.set('min_stay', String(Math.round(minStayHours * 60)));
      }
      if (!isNaN(budget) && budget > 0) {
        params.set('budget_per_candidate', String(budget));
      }

      if (btnTourSearch) btnTourSearch.disabled = true;
      if (spinner) spinner.style.display = 'inline-block';
      if (resContainer) resContainer.innerHTML = '<div style="padding: 20px; color: var(--text-muted); font-size: 13px;">Tur keşfi sonuçları taranıyor...</div>';

      try {
        const resp = await fetch(`/api/tour-explore?${params.toString()}`);
        if (!resp.ok) {
          const errData = await resp.json().catch(() => ({}));
          throw new Error(errData.message || `Sunucu hatası: HTTP ${resp.status}`);
        }
        const data = await resp.json();
        renderTourExploreResults(data);
      } catch (err) {
        showTourMsg(err.message || 'Tur keşfi sırasında bir hata oluştu.', 'error');
        if (resContainer) resContainer.innerHTML = '';
      } finally {
        if (btnTourSearch) btnTourSearch.disabled = false;
        if (spinner) spinner.style.display = 'none';
      }
    }
    if (typeof window !== 'undefined') window.runTourExplore = runTourExplore;

    function renderTourExploreResults(data) {
      const container = document.getElementById('tourResults');
      if (!container) return;

      if (!data || (!data.baseline && (!data.rows || !data.rows.length))) {
        container.innerHTML = '<div class="tp-best-empty">Uygun tur seçeneği bulunamadı.</div>';
        return;
      }

      let rowsHtml = '';

      // Baseline row
      if (data.baseline) {
        const b = data.baseline;
        const totalStr = b.true_total_try != null ? `${Math.round(b.true_total_try).toLocaleString('tr-TR')} ₺` : (b.total_try != null ? `${Math.round(b.total_try).toLocaleString('tr-TR')} ₺` : '--');
        const arriveStr = b.arrive_at ? formatTrDateTimeOrRaw(b.arrive_at) : '--';
        let nightsStr = formatNightsLine(b) || '—';
        if (nightsStr.startsWith('Geceler: ')) nightsStr = nightsStr.slice(9);
        const legs = Array.isArray(b.legs) ? b.legs : [];
        const legsHtml = legs.length ? `
          <details class="tour-legs-details">
            <summary>${legs.length} bacak (${escapeHtml(legs.map(l => l.type === 'flight' ? '✈️' : (l.type === 'train' ? '🚆' : '🚌')).join(' '))})</summary>
            <ul class="tour-legs-list">
              ${legs.map(l => `<li><strong>${escapeHtml(l.operator || l.type || '')}</strong>: ${escapeHtml(l.from)} ➔ ${escapeHtml(l.to)} (${escapeHtml(formatTrDateTimeOrRaw(l.depart_at))} – ${escapeHtml(formatTrDateTimeOrRaw(l.arrive_at))})</li>`).join('')}
            </ul>
          </details>` : '—';

        rowsHtml += `
          <tr style="background: rgba(56, 189, 248, 0.05); font-weight: 600;">
            <td><strong>🎯 Taban (Doğrudan)</strong></td>
            <td><strong style="color: var(--accent);">${escapeHtml(totalStr)}</strong></td>
            <td class="tour-diff-zero">Taban (0 ₺)</td>
            <td>${escapeHtml(arriveStr)}</td>
            <td>${escapeHtml(nightsStr)}</td>
            <td>${legsHtml}</td>
          </tr>
        `;
      }

      // Candidate rows
      const rows = Array.isArray(data.rows) ? data.rows : [];
      for (const r of rows) {
        const candName = r.candidate || '—';
        const totalStr = r.true_total_try != null ? `${Math.round(r.true_total_try).toLocaleString('tr-TR')} ₺` : (r.total_try != null ? `${Math.round(r.total_try).toLocaleString('tr-TR')} ₺` : '--');
        let diffStr = '—';
        let diffCls = 'tour-diff-zero';
        if (typeof r.diff_vs_baseline_try === 'number') {
          const diff = Math.round(r.diff_vs_baseline_try);
          if (diff > 0) {
            diffStr = `+${diff.toLocaleString('tr-TR')} ₺`;
            diffCls = 'tour-diff-pos';
          } else if (diff < 0) {
            diffStr = `${diff.toLocaleString('tr-TR')} ₺`;
            diffCls = 'tour-diff-neg';
          } else {
            diffStr = '0 ₺';
          }
        }
        const arriveStr = r.arrive_at ? formatTrDateTimeOrRaw(r.arrive_at) : '--';
        let nightsStr = formatNightsLine(r) || '—';
        if (nightsStr.startsWith('Geceler: ')) nightsStr = nightsStr.slice(9);
        const legs = Array.isArray(r.legs) ? r.legs : [];
        const legsHtml = legs.length ? `
          <details class="tour-legs-details">
            <summary>${legs.length} bacak (${escapeHtml(legs.map(l => l.type === 'flight' ? '✈️' : (l.type === 'train' ? '🚆' : '🚌')).join(' '))})</summary>
            <ul class="tour-legs-list">
              ${legs.map(l => `<li><strong>${escapeHtml(l.operator || l.type || '')}</strong>: ${escapeHtml(l.from)} ➔ ${escapeHtml(l.to)} (${escapeHtml(formatTrDateTimeOrRaw(l.depart_at))} – ${escapeHtml(formatTrDateTimeOrRaw(l.arrive_at))})</li>`).join('')}
            </ul>
          </details>` : '—';

        rowsHtml += `
          <tr>
            <td><strong>🏙️ ${escapeHtml(candName)}</strong></td>
            <td><strong>${escapeHtml(totalStr)}</strong></td>
            <td class="${diffCls}">${escapeHtml(diffStr)}</td>
            <td>${escapeHtml(arriveStr)}</td>
            <td>${escapeHtml(nightsStr)}</td>
            <td>${legsHtml}</td>
          </tr>
        `;
      }

      const skippedHtml = (Array.isArray(data.skipped) && data.skipped.length > 0)
        ? `<div class="tour-skipped" style="margin-top: 14px; font-size: 13px; color: var(--text-muted);">
            <i>ℹ️ Sonuç bulunamayan adaylar: <strong>${escapeHtml(data.skipped.join(', '))}</strong></i>
           </div>`
        : '';

      container.innerHTML = `
        <div class="tp-section-title">Tur Keşfi Sonuçları (${rows.length + (data.baseline ? 1 : 0)} seçenek)</div>
        <div class="tour-table-container">
          <table class="tour-table">
            <thead>
              <tr>
                <th>Aday Şehir</th>
                <th>Toplam Fiyat</th>
                <th>Fark</th>
                <th>Varış</th>
                <th>Geceler</th>
                <th>Bacaklar</th>
              </tr>
            </thead>
            <tbody>
              ${rowsHtml}
            </tbody>
          </table>
        </div>
        ${skippedHtml}
      `;
    }
    if (typeof window !== 'undefined') window.renderTourExploreResults = renderTourExploreResults;

    initApp();
    loadFlightHealth();
    setInterval(loadFlightHealth, 60000);
