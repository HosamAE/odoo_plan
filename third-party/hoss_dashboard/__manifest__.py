# -*- coding: utf-8 -*-
{
    "name": "Hoss App Dashboard",
    "summary": (
        "Premium Glassmorphic Dashboard & Customizer "
        "for Odoo 17, 18, and 19"
    ),
    "description": """
Hoss Premium Dashboard & Live Customizer
=========================================
Transforms the standard Odoo home menu into a beautiful, modern,
glassmorphism-inspired dashboard with live visual overrides.
    """,
    "author": "Hoss",
    "website": "https://hoss.dev",
    "support": "support@hoss.dev",
    "category": "Theme/Backend",
    "version": "19.0.1.0.0",
    "license": "OPL-1",
    "price": 39.00,
    "currency": "USD",
    "images": ["static/description/banner.png"],
    "depends": ["base", "web"],
    "data": [
        "security/ir.model.access.csv",
        "views/hoss_dashboard_config_views.xml",
        "views/dashboard_action.xml",
        "views/res_users_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "hoss_dashboard/static/src/css/home_menu.scss",
            "hoss_dashboard/static/src/js/dashboard_action.js",
            "hoss_dashboard/static/src/xml/dashboard_action.xml",
        ],
        "web.assets_web_dark": [
            "hoss_dashboard/static/src/css/home_menu_dark.scss",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
