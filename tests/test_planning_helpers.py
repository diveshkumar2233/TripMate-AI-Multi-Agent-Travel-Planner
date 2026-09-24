import re
import unittest

from backend import (
    DayPlan,
    _budget_table_section,
    _daily_budget_breakdowns,
    _flight_recommendations_section,
    _hotel_recommendations_section,
    _normalize_search_results,
)


class PlanningHelperTests(unittest.TestCase):
    def setUp(self):
        self.days = [
            DayPlan(
                day=1,
                title="Arrival and transfer",
                morning="Airport transfer and hotel check-in",
                afternoon="Rest near the hotel",
                evening="Dinner nearby",
                pro_tip="Keep your arrival details handy",
            ),
            DayPlan(
                day=2,
                title="Museum and guided tour day",
                morning="Visit a museum",
                afternoon="Take a guided tour and visit an observation deck",
                evening="Dinner in the city",
                pro_tip="Reserve timed tickets",
            ),
            DayPlan(
                day=3,
                title="Parks and market walk",
                morning="Walk through a park",
                afternoon="Browse a local market",
                evening="Relax",
                pro_tip="Keep the schedule flexible",
            ),
        ]

    def test_daily_allocations_add_up_to_the_trip_budget(self):
        result = _daily_budget_breakdowns(
            self.days, "Planning budget input: INR 100,000.", "INR"
        )
        daily_totals = [
            int(re.findall(r"(?:INR |₹)([\d,]+)", total)[0].replace(",", ""))
            for total, _ in result
        ]
        self.assertEqual(sum(daily_totals), 100_000)

    def test_activity_allocation_changes_with_day_plan(self):
        result = _daily_budget_breakdowns(
            self.days, "Planning budget input: INR 100,000.", "INR"
        )
        activity_costs = [
            int(re.search(r"Activities: (?:INR |₹)([\d,]+)", details).group(1).replace(",", ""))
            for _, details in result
        ]
        self.assertGreater(activity_costs[1], activity_costs[2])

    def test_trip_range_is_split_without_losing_the_total(self):
        result = _daily_budget_breakdowns(
            self.days, "Budget range: 80,000-140,000 (INR) for 3 days.", "INR"
        )
        low_sum = high_sum = 0
        for total, _ in result:
            amounts = [int(value.replace(",", "")) for value in re.findall(r"(?:INR |₹)([\d,]+)", total)]
            low_sum += amounts[0]
            high_sum += amounts[-1]
        self.assertEqual((low_sum, high_sum), (80_000, 140_000))

    def test_tavily_markdown_is_restored_as_linked_records(self):
        results = _normalize_search_results(
            "1. **Example Tokyo Hotel**\n   https://example.com/hotel\n   Near Ueno station."
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Example Tokyo Hotel")
        self.assertEqual(results[0]["url"], "https://example.com/hotel")

    def test_hotel_fallback_has_destination_specific_map_searches(self):
        section = _hotel_recommendations_section("No results found", "Tokyo")
        self.assertIn("Ueno / Asakusa", section)
        self.assertIn("Shinjuku / Shibuya", section)
        self.assertIn("Ginza / Marunouchi", section)
        self.assertIn("google.com/maps/search", section)
        self.assertIn("no verified names or prices", section)

    def test_hotel_source_links_survive_recommendation_rendering(self):
        section = _hotel_recommendations_section(
            "- [Example Tokyo Hotel](https://example.com/hotel) — Near Ueno", "Tokyo"
        )
        self.assertIn("[Example Tokyo Hotel](https://example.com/hotel)", section)

    def test_flight_fallback_has_route_search_and_no_fake_airlines(self):
        section = _flight_recommendations_section(
            "Live flight tracking is unavailable. No schedules or fares were returned.",
            "Japan (NRT)",
            "India (DEL)",
        )
        self.assertIn("Google Flights", section)
        self.assertIn("DEL+to+NRT", section)
        self.assertNotIn("Standard carriers", section)

    def test_budget_table_totals_match_input(self):
        table = _budget_table_section("Planning budget input: INR 100,000.", "INR", 3)
        self.assertIn("100,000", table)
        self.assertIn("100%", table)


if __name__ == "__main__":
    unittest.main()
