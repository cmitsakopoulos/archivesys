/**
 * Reading dates the way a historian writes them.
 *
 * One input accepts what an archivist would write on the folder -- "1923",
 * "May 1923", "14 May 1923", "c. 1912", "1920s", "1912-1915" -- converts it to
 * EDTF, the only form the server stores, and reads its interpretation back so
 * nothing is guessed silently. Anything it cannot read is refused, never
 * rounded to a plausible year.
 *
 * Shared by the Curation Studio and the timeline's inspector (the compiler
 * inlines this file), so the two editors can never disagree about a date.
 */
const MONTHS = ["january","february","march","april","may","june",
                "july","august","september","october","november","december"];
const MONTHS_SHORT = MONTHS.map(m => m.slice(0, 3));
const MONTH_TITLE = MONTHS.map(m => m[0].toUpperCase() + m.slice(1));

function monthIndex(word) {
  const w = word.toLowerCase().replace(/\.$/, "");
  let i = MONTHS.indexOf(w);
  if (i !== -1) return i;
  i = MONTHS_SHORT.indexOf(w.slice(0, 3));
  return i;
}

/**
 * Parse loose historical date language into EDTF.
 * Returns { edtf, reading, precision, approximate, ok }.
 */
function parseHistoricalDate(raw) {
  const input = (raw || "").trim();
  if (!input) return { edtf: "", reading: "", precision: null, approximate: false, ok: true, empty: true };

  let text = input.toLowerCase();
  let approximate = false;
  let uncertain = false;

  // circa, in every spelling anyone actually uses
  const circa = /^(c|ca|circa|about|around|approx|approximately)[.\s]+|~/;
  if (circa.test(text)) {
    approximate = true;
    text = text.replace(circa, "").trim();
  }
  if (text.endsWith("~")) { approximate = true; text = text.slice(0, -1).trim(); }
  if (text.endsWith("?")) { uncertain = true; text = text.slice(0, -1).trim(); }

  const mark = (approximate ? "~" : "") + (uncertain ? "?" : "");
  const qual = approximate && uncertain ? "approximate and uncertain"
             : approximate ? "approximate"
             : uncertain ? "uncertain" : "";

  const finish = (edtf, reading, precision) => ({
    edtf: edtf + mark,
    reading: qual ? `${reading} · ${qual}` : reading,
    precision, approximate, uncertain, ok: true, empty: false
  });

  // Range: 1912-1915, 1912–1915, 1912 to 1915, 1912/1915
  const range = text.match(/^(\d{3,4})\s*(?:[-–—\/]|to)\s*(\d{3,4})$/);
  if (range) {
    const [, a, b] = range;
    if (Number(a) <= Number(b)) {
      return finish(`${a}/${b}`, `${a} to ${b} — spans ${Number(b) - Number(a) + 1} years`, "range");
    }
  }

  // Decade: 1920s, 192X
  const decade = text.match(/^(\d{3})(?:0s|x)$/);
  if (decade) {
    const d = decade[1];
    return finish(`${d}X`, `the ${d}0s — some year in that decade`, "decade");
  }

  // ISO-ish: 1923-05-14, 1923-05, 1923
  const iso = text.match(/^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$/);
  if (iso) {
    const [, y, m, d] = iso;
    if (d) {
      const mi = Number(m) - 1;
      if (mi < 0 || mi > 11 || Number(d) < 1 || Number(d) > 31) {
        return { edtf: "", reading: `${m}/${d} is not a real month and day`, ok: false, empty: false };
      }
      return finish(`${y}-${m.padStart(2,"0")}-${d.padStart(2,"0")}`, `${Number(d)} ${MONTH_TITLE[mi]} ${y}`, "day");
    }
    if (m) {
      const mi = Number(m) - 1;
      if (mi < 0 || mi > 11) return { edtf: "", reading: `there is no month ${m}`, ok: false, empty: false };
      return finish(`${y}-${m.padStart(2,"0")}`, `${MONTH_TITLE[mi]} ${y} — a whole month`, "month");
    }
    return finish(y, `${y} — a whole year`, "year");
  }

  // Written: 14 May 1923 · May 1923 · May 14 1923 · 14 May, 1923
  const words = text.replace(/,/g, " ").split(/\s+/).filter(Boolean);
  if (words.length >= 2 && words.length <= 3) {
    let day = null, mon = -1, year = null;
    for (const w of words) {
      if (/^\d{4}$/.test(w) && year === null) { year = w; continue; }
      if (/^\d{1,2}(st|nd|rd|th)?$/.test(w) && day === null) { day = w.replace(/\D/g, ""); continue; }
      const mi = monthIndex(w);
      if (mi !== -1 && mon === -1) { mon = mi; continue; }
    }
    if (year !== null && mon !== -1) {
      const mm = String(mon + 1).padStart(2, "0");
      if (day !== null) {
        if (Number(day) < 1 || Number(day) > 31) {
          return { edtf: "", reading: `${day} is not a day of the month`, ok: false, empty: false };
        }
        return finish(`${year}-${mm}-${String(day).padStart(2,"0")}`,
                      `${Number(day)} ${MONTH_TITLE[mon]} ${year}`, "day");
      }
      return finish(`${year}-${mm}`, `${MONTH_TITLE[mon]} ${year} — a whole month`, "month");
    }
  }

  // Bare year mentioned inside prose, e.g. "spring 1923"
  const loose = text.match(/(\d{4})/);
  if (loose && words.length <= 3) {
    return finish(loose[1], `${loose[1]} — a whole year (the rest was not understood)`, "year");
  }

  return { edtf: "", reading: "not a date this can read yet", ok: false, empty: false };
}
