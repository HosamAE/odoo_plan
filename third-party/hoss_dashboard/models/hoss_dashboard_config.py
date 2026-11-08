# -*- coding: utf-8 -*-
from odoo import api, fields, models


class HossDashboardConfig(models.Model):
    _name = "hoss.dashboard.config"
    _description = "Hoss Dashboard Configuration"

    name = fields.Char(string="Name", default="Hoss Dashboard Config", required=True)
    background_type = fields.Selection(
        [("gradient", "Gradient"), ("color", "Solid Color"), ("image", "Custom Image")],
        string="Background Type",
        default="gradient",
        required=True,
    )

    background_color = fields.Char(string="Background Color", default="#f1f5f9")
    background_gradient_start = fields.Char(string="Gradient Start Color", default="#e0e7ff")
    background_gradient_end = fields.Char(string="Gradient End Color", default="#dbeafe")
    background_gradient_angle = fields.Integer(string="Gradient Angle (deg)", default=135)
    background_saturation = fields.Integer(string="Background Saturation (%)", default=100)
    background_image = fields.Image(string="Background Image")
    background_image_url_custom = fields.Char(string="Custom Background Image URL")
    preset_theme = fields.Selection(
        [
            ("custom", "Custom"),
            ("glass", "Glassmorphism"),
            ("minimal", "Minimalist"),
            ("flat", "Flat Material"),
        ],
        string="Preset Theme",
        default="custom",
    )
    background_behavior = fields.Selection(
        [("fixed", "Fixed (Static)"), ("scroll", "Scrolls with content")],
        string="Background Behavior",
        default="fixed",
        required=True,
    )
    greeting_behavior = fields.Selection(
        [("fixed", "Fixed at top"), ("scroll", "Scrolls with content")],
        string="Greeting Behavior",
        default="fixed",
        required=True,
    )

    icon_size = fields.Integer(string="Icon Size (px)", default=72)
    icon_shape = fields.Selection(
        [("rounded", "Rounded Corner"), ("circle", "Circular"), ("square", "Square")],
        string="Icon Shape",
        default="rounded",
        required=True,
    )

    show_cards = fields.Boolean(string="Show Cards Background", default=True)
    card_bg_color = fields.Char(string="Card Background Color", default="#ffffff")
    card_bg_opacity = fields.Integer(string="Card Background Opacity (%)", default=25)
    card_border_width = fields.Integer(string="Card Border Width (px)", default=1)
    card_border_color = fields.Char(string="Card Border Color", default="#ffffff")
    card_border_radius = fields.Integer(string="Card Border Radius (px)", default=28)

    greeting_title = fields.Char(string="Greeting Title", default="Welcome back!")
    greeting_subtitle = fields.Char(string="Greeting Subtitle", default="What would you like to do today?")
    greeting_align = fields.Selection([('left', 'Left'), ('center', 'Center'), ('right', 'Right')], string="Greeting Alignment", default='center', required=True)
    greeting_title_size = fields.Integer(string="Greeting Title Size (px)", default=48)
    greeting_subtitle_size = fields.Integer(string="Greeting Subtitle Size (px)", default=18)
    greeting_spacing = fields.Integer(string="Greeting Spacing (px)", default=40)
    app_order = fields.Text(string="App Order")
    lock_app_positions = fields.Boolean(string="Lock App Positions", default=False)
    top_padding = fields.Integer(string="Top Padding (px)", default=106)
    show_clock = fields.Boolean(string="Show Clock in Navbar", default=True)
    clock_text_color = fields.Char(string='Clock Text Color', default='#ffffff')
    clock_bg_color = fields.Char(string='Clock BG Color', default='#ffffff')
    clock_border_color = fields.Char(string='Clock Border Color', default='#ffffff')
    clock_bg_opacity = fields.Integer(string='Clock BG Opacity (%)', default=13)
    clock_border_width = fields.Integer(string='Clock Border Width (px)', default=1)
    clock_shadow = fields.Boolean(string='Clock Box Shadow', default=True)
    clock_text_shadow = fields.Boolean(string='Clock Text Shadow', default=True)
    use_time_schedule = fields.Boolean(string="Use Time Schedule", default=False)
    start_hour = fields.Integer(string="Start Hour (0-23)", default=8)
    end_hour = fields.Integer(string="End Hour (0-23)", default=18)

    user_id = fields.Many2one("res.users", string="User", ondelete="cascade", index=True)
    allow_user_customization = fields.Boolean(string="Allow Users to Customize", default=True)

    greeting_html = fields.Html(
        string="Greeting Message (HTML)",
        default="""<h1 class="hoss_greeting">Welcome back!</h1><p class="hoss_subtitle">What would you like to do today?</p>""",
    )

    @api.model
    def get_config(self, local_hour=None):
        is_admin = self.env.is_system() or self.env.user.has_group("base.group_system")

        # 1. Check if user is linked to a specific dashboard config
        config = self.env.user.dashboard_config_id

        # 2. If not linked, check if user has a custom config
        if not config:
            config = self.search([("user_id", "=", self.env.user.id)], limit=1)
            if config:
                self.env.user.sudo().write({"dashboard_config_id": config.id})

        # 3. If still no config, check for scheduled configs matching local_hour
        if not config and local_hour is not None:
            scheduled_configs = self.search([("user_id", "=", False), ("use_time_schedule", "=", True)])
            for sc in scheduled_configs:
                start = sc.start_hour
                end = sc.end_hour
                if start < end:
                    if start <= local_hour < end:
                        config = sc
                        break
                else:  # Overnight range, e.g. 20:00 to 06:00
                    if local_hour >= start or local_hour < end:
                        config = sc
                        break

        # 4. If still no config, fallback to default global config
        if not config:
            config = self.search([("user_id", "=", False), ("use_time_schedule", "=", False)], limit=1)

        # 5. Fallback safety creation
        if not config:
            config = self.create({"user_id": False, "use_time_schedule": False, "name": "Global Default Config"})

        # Get list of predefined systems for selection dropdown in customizer
        system_configs = self.search([("user_id", "=", False)])
        system_list = [{"id": sc.id, "name": sc.name} for sc in system_configs]

        can_customize = is_admin or self.env.user.allow_dashboard_customization

        return {
            "id": config.id,
            "name": config.name,
            "background_type": config.background_type,
            "background_behavior": config.background_behavior,
            "greeting_behavior": config.greeting_behavior,
            "background_color": config.background_color,
            "background_gradient_start": config.background_gradient_start,
            "background_gradient_end": config.background_gradient_end,
            "background_gradient_angle": config.background_gradient_angle,
            "background_saturation": config.background_saturation,
            "background_image_url": config.background_image_url_custom or (
                f"/web/image/hoss.dashboard.config/{config.id}/background_image" if config.background_image else False
            ),
            "background_image_url_custom": config.background_image_url_custom,
            "preset_theme": config.preset_theme,
            "icon_size": config.icon_size,
            "icon_shape": config.icon_shape,
            "show_cards": config.show_cards,
            "card_bg_color": config.card_bg_color,
            "card_bg_opacity": config.card_bg_opacity,
            "card_border_width": config.card_border_width,
            "card_border_color": config.card_border_color,
            "card_border_radius": config.card_border_radius,
            "greeting_title": config.greeting_title,
            "greeting_subtitle": config.greeting_subtitle,
            "greeting_align": config.greeting_align,
            "greeting_title_size": config.greeting_title_size,
            "greeting_subtitle_size": config.greeting_subtitle_size,
            "greeting_spacing": config.greeting_spacing,
            "greeting_html": config.greeting_html,  
            "is_admin": is_admin,
            "allow_user_customization": self.env.user.allow_dashboard_customization,
            "can_customize": can_customize,
            "app_order": config.app_order,
            "lock_app_positions": config.lock_app_positions,
            "top_padding": config.top_padding,
            "show_clock": config.show_clock,
            "clock_text_color": config.clock_text_color or '#ffffff',
            "clock_bg_color": config.clock_bg_color or '#ffffff',
            "clock_border_color": config.clock_border_color or '#ffffff',
            "clock_bg_opacity": config.clock_bg_opacity if config.clock_bg_opacity is not None else 13,
            "clock_border_width": config.clock_border_width if config.clock_border_width is not None else 1,
            "clock_shadow": config.clock_shadow,
            "clock_text_shadow": config.clock_text_shadow,
            "use_time_schedule": config.use_time_schedule,
            "start_hour": config.start_hour,
            "end_hour": config.end_hour,
            "systems": system_list,
            "is_custom": bool(config.user_id.id == self.env.user.id),
        }

    @api.model
    def get_config_action(self):
        is_admin = self.env.is_system() or self.env.user.has_group("base.group_system")
        if is_admin:
            return {
                "name": "Dashboard Themes & Systems",
                "type": "ir.actions.act_window",
                "res_model": "hoss.dashboard.config",
                "view_mode": "list,form",
                "views": [[False, "list"], [False, "form"]],
                "target": "current",
            }
        else:
            config_id = self.env.user.dashboard_config_id.id
            if not config_id:
                config_dict = self.get_config()
                config_id = config_dict.get("id")
            
            return {
                "name": "My Dashboard Settings",
                "type": "ir.actions.act_window",
                "res_model": "hoss.dashboard.config",
                "view_mode": "form",
                "views": [[False, "form"]],
                "res_id": config_id,
                "target": "current",
            }

    @api.model
    def save_config(self, values, save_globally=False):
        is_admin = self.env.is_system() or self.env.user.has_group("base.group_system")
        can_customize = is_admin or self.env.user.allow_dashboard_customization
        if not can_customize:
            return False

        if save_globally and is_admin:
            config = self.env.user.dashboard_config_id
            if not config or config.user_id:
                config = self.search([("user_id", "=", False), ("use_time_schedule", "=", False)], limit=1)
            if not config:
                config = self.create({"user_id": False, "use_time_schedule": False, "name": "Global Default Config"})
            config.write(values)
        else:
            user_config = self.search([("user_id", "=", self.env.user.id)], limit=1)
            if not user_config:
                vals = {**values, "user_id": self.env.user.id, "name": f"Config for {self.env.user.name}"}
                user_config = self.create(vals)
            else:
                user_config.write(values)
            self.env.user.sudo().write({"dashboard_config_id": user_config.id})
        return True

    @api.model
    def set_user_system(self, system_id, delete_custom=False):
        system = self.browse(system_id)
        if system.exists() and not system.user_id:
            self.env.user.sudo().write({"dashboard_config_id": system.id})
            if delete_custom:
                custom_config = self.search([("user_id", "=", self.env.user.id)], limit=1)
                if custom_config:
                    custom_config.unlink()
            return True
        return False

    @api.model
    def save_dashboard_app_order(self, order_dict_str):
        user_config = self.search([("user_id", "=", self.env.user.id)], limit=1)
        if not user_config:
            global_config = self.search([("user_id", "=", False)], limit=1)
            if not global_config:
                global_config = self.create({"user_id": False})
            
            vals = {
                "user_id": self.env.user.id,
                "name": f"Config for {self.env.user.name}",
                "app_order": order_dict_str
            }
            for field in global_config._fields:
                if field not in ['id', 'user_id', 'create_uid', 'create_date', 'write_uid', 'write_date', 'app_order']:
                    vals[field] = global_config[field]
            user_config = self.create(vals)
        else:
            user_config.write({"app_order": order_dict_str})
        
        self.env.user.sudo().write({"dashboard_config_id": user_config.id})
        return True
