from datetime import datetime
from decimal import Decimal

import pytest

from app.data.debt_snapshot import fold_documents


def debt(key, amount, created, terms=30):
    return {'debt_id': key, 'amount_eur': amount, 'obligation_at': datetime.fromisoformat(created), 'term_days': terms}


def test_each_document_uses_days_past_terms_instead_of_client_oldest_age():
    result = fold_documents([
        debt(1, 100, '2026-09-01T10:00:00'),  # 21 days past 30-day terms
        debt(2, 200, '2026-06-01T10:00:00'),  # 113 days past terms, same hypothetical client
        debt(3, 300, '2026-10-01T10:00:00'),  # not due yet
        debt(4, 400, '2026-09-22T10:00:00'),  # due today, not overdue
    ], '2026-10-22')
    assert result['value_at_risk_eur'] == 300
    assert result['debt_aging'][0] == {'bucket': '0-30', 'amount_eur': 100, 'count': 1}
    assert result['debt_aging'][3] == {'bucket': '90+', 'amount_eur': 200, 'count': 1}


def test_unknown_terms_are_separate_and_zero_terms_are_known():
    result = fold_documents([debt(1, 50, '2026-10-01T10:00:00', None), debt(2, 12, '2026-10-01T10:00:00', 0)], '2026-10-22')
    assert result['value_at_risk_eur'] == 12
    assert result['debt_unknown_terms_count'] == 1
    assert result['debt_unknown_terms_eur'] == 50


def test_kyiv_business_day_and_rounding_reconcile():
    # 21:30 UTC on Oct 21 is already Oct 22 in Kyiv; no overdue day yet.
    result = fold_documents([debt(1, 20, '2026-10-21T21:30:00', 0),
        debt(2, Decimal('0.005'), '2026-10-01T10:00:00', 0),
        debt(3, Decimal('0.005'), '2026-06-01T10:00:00', 0)], '2026-10-22')
    assert result['value_at_risk_eur'] == sum(row['amount_eur'] for row in result['debt_aging']) == 0.02


def test_missing_fx_is_not_zero_or_local_amount():
    with pytest.raises(ValueError, match='debt_eur_conversion_missing'):
        fold_documents([debt(1, 100, '2026-10-01T10:00:00', 0), debt(2, None, '2026-10-01T10:00:00', 0)], '2026-10-22')


def test_ambiguous_document_attachment_cannot_double_count():
    with pytest.raises(ValueError, match='attachment_ambiguous'):
        fold_documents([debt(1, 10, '2026-10-01T10:00:00', 0)] * 2, '2026-10-22')


@pytest.mark.parametrize('kind', ['debt', 'team'])
def test_failed_refresh_never_returns_expired_same_day_financial_cache(monkeypatch, kind):
    from app.api import main
    from app.core import history
    monkeypatch.setattr(main.time, 'monotonic', lambda: 10000)
    stale = {'at': 0, 'as_of': '2026-10-22', 'source_history_start': history.source_history_start().isoformat()}
    def fail(*args):
        raise ValueError('source unavailable')
    if kind == 'debt':
        stale['values'] = {1: {'value_at_risk_eur': 500}}
        monkeypatch.setattr(main, '_debt_dash_state', stale)
        monkeypatch.setattr(main.debt_snapshot, 'dashboards', fail)
        request = main._debt_dashboards_cached
    else:
        stale['value'] = {'team': {'total': 500}}
        monkeypatch.setattr(main, '_team_snap_state', stale)
        monkeypatch.setattr(main, '_build_team_snapshot', fail)
        request = main._team_snapshot_cached
    with pytest.raises(ValueError, match='source unavailable'):
        request('2026-10-22', allow_stale=True)
