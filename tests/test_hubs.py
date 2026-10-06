import pytest

from app.hubs import HubNotFound, find_hub, hubs_in_region, load_hubs


def test_hubs_load_and_are_unique():
    hubs = load_hubs()
    assert len(hubs) >= 15
    assert len({h.id for h in hubs}) == len(hubs)


@pytest.mark.parametrize(
    "query,expected",
    [("Dallas", "dallas"), ("dallas, TX", "dallas"), ("St. Louis", "st_louis"),
     ("st louis", "st_louis"), ("NYC", "newark"), ("salt-lake-city", "salt_lake_city")],
)
def test_find_hub(query, expected):
    assert find_hub(query).id == expected


def test_unknown_hub_lists_known_hubs():
    with pytest.raises(HubNotFound, match="Known hubs"):
        find_hub("Gotham")


def test_region_filter():
    midwest = {h.id for h in hubs_in_region("midwest")}
    assert {"chicago", "minneapolis"} <= midwest
    assert "miami" not in midwest
    with pytest.raises(ValueError):
        hubs_in_region("Atlantis")
