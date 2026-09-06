"""
Unit tests for CLI commands and argument parsing.
"""

from unittest.mock import MagicMock, patch
import pytest

from src.cli import main
from src.models.permit import IngestionMetrics


def test_cli_backfill_command():
    with patch("src.cli.init_pipeline") as mock_init, \
         patch("src.cli.BackfillService") as mock_service_cls:
        
        mock_service = MagicMock()
        mock_service.run.return_value = IngestionMetrics(
            total_records=100,
            new_records=95,
            updated_records=5,
            duplicate_records=0,
            min_issue_date="2024-01-01",
            max_issue_date="2024-03-01",
            duration_seconds=1.2,
        )
        mock_service_cls.return_value = mock_service
        mock_init.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with patch("sys.argv", ["cli.py", "backfill", "--days", "60"]):
            code = main()
            assert code == 0
            mock_service.run.assert_called_once_with(
                days=60,
                start_date=None,
                end_date=None,
            )


def test_cli_sync_command():
    with patch("src.cli.init_pipeline") as mock_init, \
         patch("src.cli.SyncService") as mock_service_cls:
        
        mock_service = MagicMock()
        mock_service.run.return_value = IngestionMetrics(
            total_records=10,
            new_records=8,
            updated_records=2,
            duplicate_records=0,
            max_issue_date="2024-03-02",
            duration_seconds=0.5,
        )
        mock_service_cls.return_value = mock_service
        mock_init.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with patch("sys.argv", ["cli.py", "sync", "--overlap-minutes", "90"]):
            code = main()
            assert code == 0
            mock_service.run.assert_called_once_with(overlap_minutes=90)


def test_cli_inspect_command():
    with patch("src.cli.init_pipeline") as mock_init, \
         patch("src.cli.PermitInspector") as mock_inspector_cls:
        
        mock_inspector = MagicMock()
        mock_inspector.inspect_local.return_value = {"total_permits": 0, "error": "Database contains 0 records."}
        mock_inspector_cls.return_value = mock_inspector
        mock_init.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())

        with patch("sys.argv", ["cli.py", "inspect"]):
            code = main()
            assert code == 0
            mock_inspector.inspect_local.assert_called_once()
            mock_inspector.print_report.assert_called_once()


def test_cli_export_json_command(tmp_path):
    with patch("src.cli.init_pipeline") as mock_init:
        mock_permit_store = MagicMock()
        mock_permit_store.export_to_json.return_value = 50
        mock_init.return_value = (MagicMock(), MagicMock(), MagicMock(), mock_permit_store, MagicMock())

        out_file = tmp_path / "exported.json"
        with patch("sys.argv", ["cli.py", "export-json", "--output", str(out_file)]):
            code = main()
            assert code == 0
            mock_permit_store.export_to_json.assert_called_once()
