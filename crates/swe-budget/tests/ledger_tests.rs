//! Ledger acceptance tests — the invariants from the Sweycoins strategy:
//! hard caps, idempotent charging, release on abort, conservation on
//! delegation, deterministic replay, and reward isolation.

use std::sync::{Arc, Mutex};
use std::thread;

use swe_budget::{Ledger, LedgerError};

fn funded(ledger: &mut Ledger, account: &str, amount: u64) {
    ledger
        .allocate("run", account, amount, &format!("{account}:alloc"))
        .unwrap();
}

#[test]
fn reserve_spend_and_release_are_exact() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 100_000);
    let reservation = ledger.reserve("run", "root", 20_000, "invoke-1").unwrap();

    let balance = ledger.balance("root");
    assert_eq!(balance.available(), 80_000);
    assert_eq!(balance.reserved, 20_000);
    assert_eq!(balance.spent, 0);

    // Settle 12 of 20: remainder 8 is released atomically.
    ledger.settle(reservation, 12_000, "settle-1").unwrap();
    let balance = ledger.balance("root");
    assert_eq!(balance.spent, 12_000);
    assert_eq!(balance.reserved, 0);
    assert_eq!(balance.available(), 88_000);

    let release_events = ledger
        .events()
        .iter()
        .filter(|event| matches!(event.kind, swe_budget::EventKind::Release))
        .map(|event| event.amount_milli)
        .collect::<Vec<_>>();
    assert_eq!(release_events, vec![8_000]);
}

#[test]
fn aborted_branch_releases_unspent_reservation() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 100_000);
    let reservation = ledger.reserve("run", "root", 30_000, "invoke-1").unwrap();
    assert_eq!(ledger.balance("root").available(), 70_000);

    ledger.release(reservation, "abort-1").unwrap();
    let balance = ledger.balance("root");
    assert_eq!(balance.reserved, 0);
    assert_eq!(balance.spent, 0);
    assert_eq!(balance.available(), 100_000);
}

#[test]
fn parallel_reservations_never_exceed_run_cap() {
    let ledger = Arc::new(Mutex::new(Ledger::new()));
    {
        let mut guard = ledger.lock().unwrap();
        guard.allocate("run", "root", 100_000, "run:alloc").unwrap();
    }
    let handles: Vec<_> = (0..4)
        .map(|i| {
            let ledger = Arc::clone(&ledger);
            thread::spawn(move || {
                let mut guard = ledger.lock().unwrap();
                guard.reserve("run", "root", 40_000, &format!("branch-{i}"))
            })
        })
        .collect();
    let results: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();

    // 100 available, 40 each: exactly two branches may reserve.
    let granted: Vec<u64> = results.iter().flatten().copied().collect();
    assert_eq!(granted.len(), 2);
    assert_eq!(results.iter().filter(|r| r.is_err()).count(), 2);

    // Both granted branches spend in full: total spend still capped.
    let mut guard = ledger.lock().unwrap();
    for (i, reservation) in granted.iter().enumerate() {
        guard
            .settle(*reservation, 40_000, &format!("settle-{i}"))
            .unwrap();
    }
    assert_eq!(guard.balance("root").spent, 80_000);
    assert_eq!(guard.balance("root").available(), 20_000);
    assert!(matches!(
        guard.reserve("run", "root", 21_000, "over-cap"),
        Err(LedgerError::InsufficientFunds { .. })
    ));
}

#[test]
fn idempotent_retry_charges_exactly_once() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 100_000);

    let first = ledger.reserve("run", "root", 20_000, "invoke-1").unwrap();
    let retry = ledger.reserve("run", "root", 20_000, "invoke-1").unwrap();
    assert_eq!(first, retry);

    ledger.settle(first, 20_000, "settle-1").unwrap();
    ledger.settle(first, 20_000, "settle-1").unwrap(); // retried settle: no-op

    let spends = ledger
        .events()
        .iter()
        .filter(|event| matches!(event.kind, swe_budget::EventKind::Spend))
        .count();
    assert_eq!(spends, 1);
    assert_eq!(ledger.balance("root").spent, 20_000);
}

#[test]
fn delegation_conserves_total_budget() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 100_000);
    ledger.transfer("root", "branch-a", 30_000, "t-1").unwrap();
    ledger.transfer("root", "branch-b", 30_000, "t-2").unwrap();

    assert_eq!(ledger.balance("root").available(), 40_000);
    assert_eq!(ledger.balance("branch-a").available(), 30_000);
    assert_eq!(ledger.balance("branch-b").available(), 30_000);

    let total: u64 = ["root", "branch-a", "branch-b"]
        .iter()
        .map(|account| ledger.balance(account).available())
        .sum();
    assert_eq!(total, 100_000);

    assert!(matches!(
        ledger.transfer("root", "branch-b", 41_000, "over"),
        Err(LedgerError::InsufficientFunds { .. })
    ));
}

#[test]
fn reward_never_increases_spendable_budget() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 10_000);
    ledger.reward("run", "root", 80_000, "gold-1").unwrap();

    assert_eq!(ledger.rewards("root"), 80_000);
    assert_eq!(ledger.balance("root").available(), 10_000);
    assert!(matches!(
        ledger.reserve("run", "root", 11_000, "over"),
        Err(LedgerError::InsufficientFunds { .. })
    ));
}

#[test]
fn negative_balance_and_overspend_attempts_rejected() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 10_000);

    assert!(matches!(
        ledger.reserve("run", "root", 10_001, "too-big"),
        Err(LedgerError::InsufficientFunds { .. })
    ));
    assert!(matches!(
        ledger.transfer("root", "other", 10_001, "too-big-transfer"),
        Err(LedgerError::InsufficientFunds { .. })
    ));

    let reservation = ledger.reserve("run", "root", 5_000, "ok").unwrap();
    assert!(matches!(
        ledger.settle(reservation, 5_001, "overspend"),
        Err(LedgerError::Overspend { .. })
    ));
    ledger.settle(reservation, 5_000, "settle").unwrap();
    assert!(matches!(
        ledger.settle(reservation, 5_000, "settle-again"),
        Err(LedgerError::ReservationAlreadySettled(..))
    ));
}

#[test]
fn key_reuse_with_different_parameters_rejected() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 10_000);
    ledger.reserve("run", "root", 3_000, "key").unwrap();
    assert!(matches!(
        ledger.reserve("run", "root", 4_000, "key"),
        Err(LedgerError::KeyConflict(..))
    ));
}

#[test]
fn replay_reconstructs_identical_state_and_allows_resume() {
    let mut ledger = Ledger::new();
    funded(&mut ledger, "root", 100_000);
    let open = ledger.reserve("run", "root", 20_000, "open").unwrap();
    let done = ledger.reserve("run", "root", 30_000, "done").unwrap();
    ledger.settle(done, 12_000, "settle-done").unwrap();
    ledger
        .transfer("root", "branch-a", 25_000, "delegate")
        .unwrap();
    ledger.reward("run", "root", 40_000, "gold").unwrap();

    // "Crash": reconstruct from the log alone.
    let mut replayed = Ledger::replay(ledger.events()).unwrap();
    assert_eq!(replayed.balances(), ledger.balances());
    assert_eq!(replayed.rewards("root"), ledger.rewards("root"));
    assert_eq!(replayed.events(), ledger.events());

    // The open reservation survives the crash and can still settle — without
    // double charging the earlier events.
    replayed.settle(open, 7_000, "settle-after-crash").unwrap();
    assert_eq!(replayed.balance("root").spent, 12_000 + 7_000);
    let spends = replayed
        .events()
        .iter()
        .filter(|event| matches!(event.kind, swe_budget::EventKind::Spend))
        .count();
    assert_eq!(spends, 2);
}
