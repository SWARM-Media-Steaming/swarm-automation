//! Time source, injectable so session expiry and month rollover are testable.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub trait Clock: Send + Sync {
    fn now_secs(&self) -> u64;
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now_secs(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
    }
}

/// A clock a test moves by hand.
pub struct ManualClock(AtomicU64);

impl ManualClock {
    pub fn new(secs: u64) -> Self {
        ManualClock(AtomicU64::new(secs))
    }

    pub fn set(&self, secs: u64) {
        self.0.store(secs, Ordering::SeqCst);
    }

    pub fn advance(&self, secs: u64) {
        self.0.fetch_add(secs, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now_secs(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}

/// `YYYY-MM` (UTC) of a Unix timestamp: the key of a monthly spend period.
pub fn period_of(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    // Howard Hinnant's civil-from-days algorithm.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn periods_follow_the_utc_calendar() {
        assert_eq!(period_of(0), "1970-01");
        assert_eq!(period_of(951_782_400), "2000-02"); // 2000-02-29, a leap day
        assert_eq!(period_of(1_709_251_199), "2024-02"); // 2024-02-29 23:59:59
        assert_eq!(period_of(1_709_251_200), "2024-03");
        assert_eq!(period_of(1_767_225_599), "2025-12");
        assert_eq!(period_of(1_767_225_600), "2026-01");
    }
}
