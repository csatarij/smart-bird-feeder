#!/usr/bin/env python3
"""Tests for the stats engine module."""

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from stats_engine import StatsEngine
from utils import load_config


@pytest.fixture
def config():
    return load_config()


class TestStatsEngine:
    """Test the statistics recording and querying."""

    def test_record_and_retrieve(self):
        """Recording a sighting should be retrievable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config()
            config["storage"]["database_path"] = f"{tmpdir}/test.db"
            config["storage"]["stats_dir"] = f"{tmpdir}/stats"

            stats = StatsEngine(config)
            stats.record_sighting(
                species="Blue Tit",
                confidence=0.92,
                image_path="data/classified/blue_tit/test.jpg",
                predictions=[
                    {"species": "Blue Tit", "confidence": 0.92},
                    {"species": "Great Tit", "confidence": 0.05},
                ],
            )

            summary = stats.get_daily_summary()
            assert summary["total_sightings"] == 1
            assert "Blue Tit" in summary["species"]
            assert summary["species"]["Blue Tit"]["count"] == 1

            stats.close()

    def test_multiple_species(self):
        """Multiple species should all appear in stats."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config()
            config["storage"]["database_path"] = f"{tmpdir}/test.db"
            config["storage"]["stats_dir"] = f"{tmpdir}/stats"

            stats = StatsEngine(config)
            for species in ["Robin", "Robin", "Blue Jay", "Cardinal"]:
                stats.record_sighting(species=species, confidence=0.8)

            summary = stats.get_daily_summary()
            assert summary["total_sightings"] == 4
            assert summary["unique_species"] == 3
            assert summary["species"]["Robin"]["count"] == 2

            stats.close()

    def test_all_time_stats(self):
        """All-time stats should aggregate correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config()
            config["storage"]["database_path"] = f"{tmpdir}/test.db"
            config["storage"]["stats_dir"] = f"{tmpdir}/stats"

            stats = StatsEngine(config)
            for _ in range(5):
                stats.record_sighting(species="Sparrow", confidence=0.75)

            all_time = stats.get_all_time_stats()
            assert all_time["total_sightings"] == 5
            assert all_time["unique_species"] == 1
            assert all_time["species_ranking"][0]["species"] == "Sparrow"

            stats.close()

    def test_json_export(self):
        """Daily summary should export as valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config()
            config["storage"]["database_path"] = f"{tmpdir}/test.db"
            config["storage"]["stats_dir"] = f"{tmpdir}/stats"

            stats = StatsEngine(config)
            stats.record_sighting(species="Wren", confidence=0.65)
            path = stats.export_daily_summary()

            assert path.exists()
            with open(path) as f:
                data = json.load(f)
            assert data["total_sightings"] == 1

            stats.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
