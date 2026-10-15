"""
Structured logging for the Falcon Research Engine.
Provides structured logging and metrics tracking.
"""

import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional

class FalconLogger:
    """Structured logger for the Falcon Research Engine."""

    def __init__(self, name: str = "falcon"):
        self.logger = logging.getLogger(name)
        
        # Set up basic logging configuration if not already done
        if not self.logger.handlers:
            # Create console handler with JSON formatting
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
    
    def info(self, message: str, **kwargs):
        """Log info level message."""
        if kwargs:
            self.logger.info(f"{message} {json.dumps(kwargs)}")
        else:
            self.logger.info(message)
    
    def warning(self, message: str, **kwargs):
        """Log warning level message."""
        if kwargs:
            self.logger.warning(f"{message} {json.dumps(kwargs)}")
        else:
            self.logger.warning(message)
            
    def error(self, message: str, **kwargs):
        """Log error level message."""
        if kwargs:
            self.logger.error(f"{message} {json.dumps(kwargs)}")
        else:
            self.logger.error(message)
    
    def debug(self, message: str, **kwargs):
        """Log debug level message."""
        if kwargs:
            self.logger.debug(f"{message} {json.dumps(kwargs)}")
        else:
            self.logger.debug(message)

# Create a global logger instance
bot_logger = FalconLogger()


# Legacy alias (pre-v0.1.0 name; kept for compatibility, do not use in new code).
ScalpingBotLogger = FalconLogger

def log_trade(trade_data: Dict[str, Any]):
    """Log trade information."""
    bot_logger.info("Trade executed", **trade_data)

def log_portfolio_update(portfolio_data: Dict[str, Any]):
    """Log portfolio updates."""
    bot_logger.info("Portfolio updated", **portfolio_data)

def log_signal_generation(signal_data: Dict[str, Any]):
    """Log signal generation."""
    bot_logger.info("Signal generated", **signal_data)

def log_risk_event(event_type: str, details: Dict[str, Any]):
    """Log risk-related events."""
    bot_logger.warning(f"Risk event: {event_type}", **details)
    
def log_system_status(status_data: Dict[str, Any]):
    """Log system status information."""
    bot_logger.info("System status", **status_data)

# Create logging utility functions
def setup_logging(log_level: str = "INFO"):
    """Setup logging with specified level."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f'Invalid log level: {log_level}')
    
    bot_logger.logger.setLevel(numeric_level)