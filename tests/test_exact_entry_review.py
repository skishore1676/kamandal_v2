from kamandal_v2.intelligence.exact_entry_review import possible_edit_duplicates, build_review
from kamandal_v2.intelligence.source_episode_compiler import _hold_possible_edits


def record(ref, time='2026-09-17T15:07:37Z', text='Downside hedge https://t.co/a', sha='abc'):
    return {'signal_id':ref,'profile_id':'mike','literal':{'text':text},'source':{'published_at':time,'media':[{'sha256':sha}]}}


def test_duplicate_versions_held_but_distinct_evidence_retained():
    a=record('a');b=record('b','2026-09-17T15:07:51Z','Downside hedge https://t.co/b')
    assert possible_edit_duplicates([a,b])=={'a':['b'],'b':['a']}
    assert possible_edit_duplicates([a,record('c',sha='different')])=={}
    assert possible_edit_duplicates([a,record('d',time='2026-09-18T15:07:37Z')])=={}
    episode={'events':[{'planner_new_entry':True,'blockers':[], 'projection_dispositions':[{'projection':'exact_package','disposition':'ready_for_source_policy'}]}]}
    held=_hold_possible_edits(episode,['b'])
    assert held['events'][0]['planner_new_entry'] is False
    assert held['events'][0]['projection_dispositions'][0]['disposition']=='parked'
    assert episode['events'][0]['planner_new_entry'] is True


def test_review_does_not_call_complete_covered_option_an_authorized_trade():
    event={'event_id':'e','action':'open','symbol':'IBIT','structure_hint':'covered_call','evidence_status':'complete','exact_packages':[{'complete':True,'legs':[{'expiration':'Oct 16 2026'}]}]}
    report=build_review({'records':[record('a')]},{'episodes':[{'post_ref':'a','events':[event]}]})
    row=report['rows'][0]
    assert 'existing_share_coverage_requires_verification' in row['review_blockers']
    assert row['live_capability']=='unsupported_live_exact_structure'
    assert row['execution_authorized'] is False
    assert not any(report['effects'].values())


def test_template_and_unresolved_month_remain_blocked():
    event={'event_id':'e','action':'open','symbol':'AMGN','template_number':4,'evidence_status':'complete','exact_packages':[{'complete':True,'legs':[{'expiration':'Oct 2026 standard monthly'}]}]}
    row=build_review({'records':[record('a')]},{'episodes':[{'post_ref':'a','events':[event]}]})['rows'][0]
    assert 'expiration_date_unresolved' in row['review_blockers']
    assert 'template_not_confirmed_contract_entry' in row['review_blockers']


def test_complete_contract_is_reviewable_not_execution_authorized():
    event={'event_id':'e','action':'open','symbol':'SPX','structure_hint':'long_call','evidence_status':'complete','exact_packages':[{'complete':True,'legs':[{'expiration':'Sep 30 2026','strike':'7400','quantity':1,'order_code':'BTO','option_type':'put'}]}]}
    row=build_review({'records':[record('a')]},{'episodes':[{'post_ref':'a','events':[event]}]})['rows'][0]
    assert row['translation_status']=='reviewable_contracts'
    assert row['execution_authorized'] is False
    event['exact_packages'][0]['legs'][0]['quantity']=0
    row=build_review({'records':[record('a')]},{'episodes':[{'post_ref':'a','events':[event]}]})['rows'][0]
    assert 'invalid_or_nonopening_contract_leg' in row['review_blockers']
