"""Made-up data for the demo, for a fictional home goods retailer. Every value is synthetic."""

CHANNEL_SPEND: dict[str, float] = {"paid_search": 48210.55, "social": 31877.10, "display": 12950.00}
DAILY_SPEND: dict[str, list[float]] = {
    "paid_search": [6800.0, 7100.0, 6950.0, 7020.0, 6880.0, 7240.0, 6220.55],
    "social": [4510.0, 4620.0, 4400.0, 4700.0, 4555.0, 4590.0, 4502.1],
}
# The server's own record of who works where; "team" is the Team's External ID in Emet
USERS: dict[str, dict[str, str]] = {
    "maya@marlowhome.example": {"team": "marketing"},
    "theo@marlowhome.example": {"team": "ecommerce"},
}
