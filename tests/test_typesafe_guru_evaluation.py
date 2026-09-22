import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('typesafe_eval', Path(__file__).parents[1]/'scripts/evaluate_typesafe_gurus.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def record(text, profile='mike_butler'):
    return {'signal_id': 'p', 'profile_id': profile, 'source': {'published_at': '2026-01-01'},
            'literal': {'text': text}, 'expected_ideas': [{'symbol': 'SECRET_LABEL'}]}


def test_no_label_leak_and_mixed_symbols():
    request = m.build_request(record('Closed $WDC. New call diagonal in $PYPL'), [record('$OTHER', 'greg_harmon')])
    assert set(request['questions']) == {'needs_more','new_entry','symbol_WDC','symbol_PYPL'}
    assert request['state']['earlier_posts'] == []
    assert 'SECRET_LABEL' not in str(request)


def test_context_bounded_and_no_numeric_dollar_amount():
    req = m.build_request(record('Closed $235 winner $SNOW'), [record(str(i)) for i in range(20)])
    assert len(req['state']['earlier_posts']) == 10
    assert 'symbol_235' not in req['questions']


def test_missing_response_fails_closed():
    with pytest.raises(ValueError):
        m.validate_response(m.build_request(record('$SPY'), []), {'answers': {}})


def test_unknown_never_becomes_trade():
    assert m.emitted({'response': {'answers': {'symbol_SPY': {'choice': 'unknown'},
        'symbol_WDC': {'choice': 'none'}, 'symbol_PYPL': {'choice': 'bullish'}}}}) == {('PYPL','bullish')}


def test_out_of_range_probability_rejected():
    request = {'questions': {'q': {'type': 'noul'}}}
    with pytest.raises(ValueError):
        m.validate_response(request, {'answers': {'q': {'type': 'noul', 'noul': 2}}, 'usage': {'input_tokens': 1}})
