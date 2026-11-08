# -*- coding: utf-8 -*-
from odoo import models, fields

class ResUsers(models.Model):
    _inherit = "res.users"

    allow_dashboard_customization = fields.Boolean(
        string="Allow Dashboard Customization",
        help="Check this to allow this user to customize their dashboard even if they are not an administrator."
    )
    
    can_customize_dashboard = fields.Boolean(
        string="Can Customize Dashboard",
        compute="_compute_can_customize_dashboard"
    )

    def _compute_can_customize_dashboard(self):
        for user in self:
            is_admin = user.has_group("base.group_system") or user.id == 1
            user.can_customize_dashboard = is_admin or user.allow_dashboard_customization
    
    dashboard_config_id = fields.Many2one(
        "hoss.dashboard.config",
        string="Dashboard Theme/System",
        ondelete="set null"
    )
