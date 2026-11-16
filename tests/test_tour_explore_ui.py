"""Tests for Tour Explore UI (T18 - PLN-15b)."""

import os
import subprocess
import unittest


class TestTourExploreUi(unittest.TestCase):
    def setUp(self):
        self.static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        with open(os.path.join(self.static_dir, "index.html"), "r", encoding="utf-8") as f:
            self.html = f.read()
        with open(os.path.join(self.static_dir, "js", "app.js"), "r", encoding="utf-8") as f:
            self.js = f.read()

    def test_html_contains_tour_explore_tab_and_inputs(self):
        """1. HTML içinde Tur Keşfi sekmesi ve gerekli tüm girdiler bulunmalı."""
        # Tab button / navigation
        self.assertIn("tabBtnTourExplore", self.html)
        self.assertIn("Tur Keşfi", self.html)

        # Section panel
        self.assertIn('id="tourExploreSection"', self.html)

        # Required input fields:
        # kalkış, varış, tarihler, son varış, aday şehirler, en az kalış, aday başına bütçe
        self.assertIn('id="tourOrigin"', self.html)
        self.assertIn('id="tourDestination"', self.html)
        self.assertIn('id="tourDateStart"', self.html)
        self.assertIn('id="tourDateEnd"', self.html)
        self.assertIn('id="tourArriveDate"', self.html)
        self.assertIn('id="tourCandidates"', self.html)
        self.assertIn('id="tourMinStayHours"', self.html)
        self.assertIn('id="tourBudget"', self.html)

    def test_html_contains_warning_and_search_button(self):
        """2. Uyarı satırı ve arama butonu bulunmalı."""
        self.assertIn("Her aday Google araması harcar (aday × bütçe)", self.html)
        self.assertIn('id="btnTourSearch"', self.html)

    def test_js_contains_tour_explore_functions(self):
        """3. app.js içinde arama tetikleyici, sekme geçişi ve sonuç tablosu işleyicileri olmalı."""
        self.assertIn("function runTourExplore", self.js)
        self.assertIn("function renderTourExploreResults", self.js)
        self.assertIn("/api/tour-explore", self.js)

    def test_button_disabled_while_searching_in_js(self):
        """4. Arama sürerken düğme kilitlenmeli (disabled)."""
        self.assertIn("btnTourSearch.disabled = true", self.js)
        self.assertIn("btnTourSearch.disabled = false", self.js)

    def test_mock_render_and_html_dump(self):
        """5. Sahte sonuçla tablo oluşturulabilmeli ve HTML dökümü doğrulanabilmeli."""
        node_script = """
const fs = require('fs');
let code = fs.readFileSync('static/js/app.js', 'utf8');
const container = { innerHTML: '' };
const dummy = { value: '', classList: { add: () => {}, remove: () => {}, toggle: () => {} }, style: {}, addEventListener: () => {}, setAttribute: () => {}, querySelectorAll: () => [] };
const mockDoc = {
  getElementById: (id) => id === 'tourResults' ? container : dummy,
  querySelector: () => dummy,
  querySelectorAll: () => [],
  body: dummy
};
const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
const setInterval = () => {};
const window = { document: mockDoc, localStorage };
const document = mockDoc;
eval(code);

const mock_data = {
  baseline: {
    candidate: null,
    total_try: 8272.0,
    true_total_try: 8272.0,
    depart_at: '2026-10-25T18:00',
    arrive_at: '2026-10-26T15:00',
    diff_vs_baseline_try: 0.0,
    nights: [{ date: '2026-10-26', type: 'transit' }],
    legs: [{ type: 'flight', operator: 'Aegean', from: 'ATH', to: 'PRG', depart_at: '2026-10-25T18:00', arrive_at: '2026-10-26T15:00' }]
  },
  rows: [
    {
      candidate: 'Krakov',
      total_try: 8654.0,
      true_total_try: 8654.0,
      depart_at: '2026-10-25T18:00',
      arrive_at: '2026-10-27T08:00',
      diff_vs_baseline_try: 382.0,
      nights: [
        { date: '2026-10-26', type: 'stay', place: 'Krakov' },
        { date: '2026-10-27', type: 'transit' }
      ],
      legs: [
        { type: 'flight', operator: 'Ryanair', from: 'ATH', to: 'KRK', depart_at: '2026-10-25T18:00', arrive_at: '2026-10-25T20:30' },
        { type: 'train', operator: 'PKP', from: 'KRK', to: 'PRG', depart_at: '2026-10-26T22:00', arrive_at: '2026-10-27T08:00' }
      ]
    },
    {
      candidate: 'Budapeşte',
      total_try: 10597.0,
      true_total_try: 10597.0,
      depart_at: '2026-10-25T18:00',
      arrive_at: '2026-10-28T06:00',
      diff_vs_baseline_try: 2325.0,
      nights: [
        { date: '2026-10-26', type: 'stay', place: 'Budapeşte' },
        { date: '2026-10-27', type: 'stay', place: 'Budapeşte' },
        { date: '2026-10-28', type: 'transit' }
      ],
      legs: [
        { type: 'flight', operator: 'Wizz Air', from: 'ATH', to: 'BUD', depart_at: '2026-10-25T18:00', arrive_at: '2026-10-25T19:50' },
        { type: 'bus', operator: 'FlixBus', from: 'Budapeşte', to: 'Prag', depart_at: '2026-10-27T23:00', arrive_at: '2026-10-28T06:00' }
      ]
    }
  ],
  skipped: ['Viyana'],
  candidates: ['Krakov', 'Budapeşte', 'Viyana']
};

window.renderTourExploreResults(mock_data);
const outDir = 'docs/agent/reports/T18';
if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(outDir + '/tour_explore_ui_mock.html', container.innerHTML, 'utf8');
console.log('OK');
"""
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("OK", proc.stdout)

        dump_path = os.path.join(os.path.dirname(__file__), "..", "docs", "agent", "reports", "T18", "tour_explore_ui_mock.html")
        self.assertTrue(os.path.exists(dump_path), "HTML dump file must exist")
        with open(dump_path, "r", encoding="utf-8") as f:
            dump_html = f.read()
        self.assertIn("tour-table", dump_html)
        self.assertIn("Krakov", dump_html)
        self.assertIn("Budapeşte", dump_html)
        self.assertIn("8.272 ₺", dump_html)


if __name__ == "__main__":
    unittest.main()
