# -*- coding: utf-8 -*-
{
    'name': 'Enterprise Theme for Community',

    'summary': 'Enterprise-style Odoo 19 web client for Community — home menu, purple theme, global search, and dark mode.',

    'description': """
Enterprise Theme for Community
==============================

Bring the Odoo Enterprise look and feel to Odoo 19 Community Edition.

Key capabilities
----------------
* Enterprise purple color scheme and polished UI components
* Home menu app grid on login with drag-and-drop app ordering
* Global search from the home menu (opens the command palette)
* Ctrl+K / Cmd+K command palette shortcut across the backend
* Enterprise-style navigation bar with app grid toggle
* Light, Dark, and System theme per user preference
* Enhanced list, kanban, form, pivot, and login screen styling

Requirements
------------
* Odoo 19 Community Edition
* Do not install together with Odoo Enterprise ``web_enterprise`` (conflicting web client)
    """,

    'author': 'Zarixsol',
    'website': 'https://www.zarixsol.com',
    'maintainer': 'Rizwan Chaudhary',
    'support': 'info@zarixsol.com',

    'version': '19.0.1.0.2',
    'license': 'LGPL-3',
    'category': 'Productivity',

    'depends': [
        'web',
        'base_setup',
    ],

    'data': [
        'views/webclient_templates.xml',
        'views/res_users_views.xml',
    ],

    'assets': {
        'web._assets_primary_variables': [
            ('after', 'web/static/src/scss/primary_variables.scss', 'zxs_entp_theme/static/src/**/*.variables.scss'),
            ('before', 'web/static/src/scss/primary_variables.scss', 'zxs_entp_theme/static/src/scss/primary_variables.scss'),
        ],
        'web._assets_secondary_variables': [
            ('before', 'web/static/src/scss/secondary_variables.scss', 'zxs_entp_theme/static/src/scss/secondary_variables.scss'),
        ],
        'web._assets_backend_helpers': [
            ('before', 'web/static/src/scss/bootstrap_overridden.scss', 'zxs_entp_theme/static/src/scss/bootstrap_overridden.scss'),
        ],
        'web.assets_frontend': [
            'zxs_entp_theme/static/src/webclient/home_menu/home_menu_background.scss',
            'zxs_entp_theme/static/src/webclient/navbar/navbar.scss',
        ],
        'web.assets_backend': [
            'zxs_entp_theme/static/src/webclient/**/*.scss',
            'zxs_entp_theme/static/src/views/**/*.scss',
            'zxs_entp_theme/static/src/core/**/*.scss',
            'zxs_entp_theme/static/src/search/**/*.scss',
            'zxs_entp_theme/static/src/core/**/*',
            'zxs_entp_theme/static/src/webclient/**/*.js',
            'zxs_entp_theme/static/src/webclient/**/*.xml',
            ('remove', 'zxs_entp_theme/static/src/views/pivot/**'),
            ('remove', 'zxs_entp_theme/static/src/**/*.dark.scss'),
        ],
        'web.assets_backend_lazy': [
            'zxs_entp_theme/static/src/views/pivot/**',
        ],
        'web.assets_backend_lazy_dark': [
            ('include', 'web.dark_mode_variables'),
            ('before', 'zxs_entp_theme/static/src/scss/bootstrap_overridden.scss', 'zxs_entp_theme/static/src/scss/bootstrap_overridden.dark.scss'),
            ('after', 'web/static/lib/bootstrap/scss/_functions.scss', 'zxs_entp_theme/static/src/scss/bs_functions_overridden.dark.scss'),
        ],
        'web.assets_web': [
            ('replace', 'web/static/src/main.js', 'zxs_entp_theme/static/src/main.js'),
        ],
        'web.dark_mode_variables': [
            ('before', 'zxs_entp_theme/static/src/scss/primary_variables.scss', 'zxs_entp_theme/static/src/scss/primary_variables.dark.scss'),
            ('before', 'zxs_entp_theme/static/src/**/*.variables.scss', 'zxs_entp_theme/static/src/**/*.variables.dark.scss'),
            ('before', 'zxs_entp_theme/static/src/scss/secondary_variables.scss', 'zxs_entp_theme/static/src/scss/secondary_variables.dark.scss'),
        ],
        'web.assets_web_dark': [
            ('include', 'web.dark_mode_variables'),
            ('before', 'zxs_entp_theme/static/src/scss/bootstrap_overridden.scss', 'zxs_entp_theme/static/src/scss/bootstrap_overridden.dark.scss'),
            ('after', 'web/static/lib/bootstrap/scss/_functions.scss', 'zxs_entp_theme/static/src/scss/bs_functions_overridden.dark.scss'),
            'zxs_entp_theme/static/src/**/*.dark.scss',
        ],
    },

    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
