import pytest
from remi.clock import resolve_time
from remi.identity import source_name_present

@pytest.mark.parametrize('name,text,expected',[('Ann','Anniversary celebration',False),('Ann','Ann visited',True),
    ('Lee','Leeds office',False),('李明','李明参加',True),('R&D','R&D group',True)])
def test_source_names_use_generic_literal_boundaries(name,text,expected):
    assert source_name_present(name.casefold(),text.casefold()) is expected


@pytest.mark.parametrize('phrase,text',[('2024年6月9日','6月9日完成'),('2024-06-09','June 9 completed'),('June 9','June 9 completed')])
def test_equivalent_date_encodings_are_source_grounded(phrase,text):
    assert resolve_time(phrase,text,'2024-06-02T10:00:00Z').startswith('2024-06-09')
    assert resolve_time(phrase,'No date here.','2024-06-02T10:00:00Z') is None


def test_equivalent_english_year_is_grounded_in_source_month_day():
    assert resolve_time('March 8, 2027','Booked for March 8.','2027-03-01T10:00:00Z').startswith('2027-03-08')
    assert resolve_time('March 8, 2028','Booked for March 8.','2027-03-01T10:00:00Z') is None


@pytest.mark.parametrize('phrase,source',[('March 12, 2027','Completed today.'),('2027年3月12日','今天完成。')])
def test_normalized_calendar_date_is_equivalent_to_source_relative_day(phrase,source):
    assert resolve_time(phrase,source,'2027-03-12T10:00:00Z').startswith('2027-03-12')
    assert resolve_time(phrase,source,'2027-03-13T10:00:00Z') is None


