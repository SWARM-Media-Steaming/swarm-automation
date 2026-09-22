(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmPromptGrades = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  // Pure helpers for the Feedback view's prompt-grades panel. Best first; the
  // order matches the router's allowed grades.
  const GRADES = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"];

  // Grade letter -> the shared state vocabulary (.suite-state passed/running/
  // blocked/failed) so grades reuse the app's existing status colors.
  function gradeTone(grade) {
    const letter = String(grade || "").trim().charAt(0).toUpperCase();
    if (letter === "A") return "passed";
    if (letter === "B") return "running";
    if (letter === "C") return "blocked";
    if (letter === "D" || letter === "F") return "failed";
    return "";
  }

  // One bar per grade, in grade order, sized against the most common grade so
  // the tallest bar always fills the track. Grades nobody earned are kept (at
  // zero) so the scale never shifts between repositories. `selectedGrade`
  // marks the bar that is currently filtering the list.
  function distributionBars(summary, selectedGrade) {
    const counts = (summary && summary.distribution) || {};
    const selected = String(selectedGrade || "");
    const largest = Math.max(0, ...GRADES.map((grade) => Number(counts[grade]) || 0));
    return GRADES.map((grade) => {
      const count = Number(counts[grade]) || 0;
      return {
        grade,
        count,
        percent: largest ? Math.round((count / largest) * 100) : 0,
        tone: gradeTone(grade),
        selected: grade === selected,
      };
    });
  }

  // Clicking the active grade again clears the filter. Anything that is not a
  // real grade leaves the current filter alone.
  function toggleGrade(current, grade) {
    const next = String(grade || "");
    if (!GRADES.includes(next)) return String(current || "");
    return current === next ? "" : next;
  }

  // Tabs inside the Feedback view. The order is the order they appear in, and
  // the first entry is what an unknown/empty tab falls back to.
  const FEEDBACK_TABS = ["grades", "routing", "history"];

  function activeTab(tab) {
    const next = String(tab || "");
    return FEEDBACK_TABS.includes(next) ? next : FEEDBACK_TABS[0];
  }

  // Router rows for the "who grades, and who do they pick" panel. Each row is
  // one grading platform with its selections already sized against its own
  // total, so a row answers "when this platform grades, who does it choose?"
  // `selectedRouter` marks the row currently filtering the grades list.
  // Rows whose router was never recorded stay visible but cannot filter —
  // there is no value to filter on, and dropping them would make the totals
  // disagree with the grade count.
  function routerRows(matrix, selectedRouter) {
    const rows = Array.isArray(matrix) ? matrix : [];
    const selected = String(selectedRouter || "");
    return rows.map((row) => {
      const router = String((row && row.router) || "");
      const graded = Number(row && row.graded) || 0;
      const selections = Array.isArray(row && row.selections) ? row.selections : [];
      return {
        router,
        graded,
        interactive: router.length > 0,
        selected: router.length > 0 && router === selected,
        selections: selections.map((entry) => {
          const count = Number(entry && entry.count) || 0;
          const percent = Number(entry && entry.percent);
          return {
            provider: String((entry && entry.provider) || ""),
            count,
            percent: Number.isFinite(percent) ? percent : (graded ? (count / graded) * 100 : 0),
          };
        }),
      };
    });
  }

  // Clicking the active router again clears the filter. A router that is not
  // in the matrix (or the blank "not recorded" row) leaves the filter alone.
  function toggleRouter(current, router, matrix) {
    const next = String(router || "");
    const known = routerRows(matrix, "").some((row) => row.interactive && row.router === next);
    if (!known) return String(current || "");
    return current === next ? "" : next;
  }

  // "Codex graded 12 of 30 prompts"-style headline for one router row.
  function routerLine(row, label) {
    const graded = Number(row && row.graded) || 0;
    const top = (row && row.selections && row.selections[0]) || null;
    if (!graded || !top) return "No graded prompts yet.";
    const share = Math.round(Number(top.percent) || 0);
    const name = label ? label(top.provider) : top.provider;
    return `${graded} graded · picked ${name} ${share}% of the time`;
  }

  // "3 of 12 prompts graded B or better"-style headline used under the average.
  function summaryLine(summary) {
    const graded = Number(summary && summary.graded) || 0;
    if (!graded) return "No graded prompts yet.";
    const counts = summary.distribution || {};
    const strong = GRADES.filter((grade) => grade[0] === "A" || grade[0] === "B")
      .reduce((total, grade) => total + (Number(counts[grade]) || 0), 0);
    return `${strong} of ${graded} graded prompt${graded === 1 ? "" : "s"} earned a B or better.`;
  }

  return {
    GRADES,
    FEEDBACK_TABS,
    activeTab,
    gradeTone,
    distributionBars,
    toggleGrade,
    routerRows,
    toggleRouter,
    routerLine,
    summaryLine,
  };
});
