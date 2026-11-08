/** @odoo-module **/
import { registry, Registry } from "@web/core/registry";
import { Component, onWillStart, useState, onMounted, onWillUnmount, useRef, markup, onPatched, xml } from "@odoo/owl";
import { useService, useBus } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";

let cachedConfig = null;
let originalOrderDict = null;

export class hossDashboard extends Component {
    static template = "hoss_app_dashboard.Dashboard";
    
    setup() {
        this.menuService = useService("menu");
        this.actionService = useService("action");
        this.orm = useService("orm");
        this.command = useService("command");
        this.dashboardContainer = useRef("dashboardContainer");
        
        this.state = useState({
            apps: [],
            config: {},
            tempConfig: {},
            draggedApp: null,
            searchQuery: "",
            customizerOpen: false,
            saveGlobally: false,
            currentTime: "",
            currentDate: ""
        });

        onWillStart(async () => {
            const apps = this.menuService.getApps();
            const localHour = new Date().getHours();
            
            // Always fetch fresh — never serve stale config from previous session
            const config = await this.orm.call("hoss.dashboard.config", "get_config", [localHour]);
            cachedConfig = config;
            
            let orderDict = {};
            try {
                orderDict = JSON.parse(config.app_order || "{}");
            } catch (e) {
                orderDict = {};
            }
            // Snapshot order so onDragEnd can detect real changes
            originalOrderDict = { ...orderDict };

            if (config.greeting_html) {
                config.greeting_html = markup(config.greeting_html);
            }

            apps.sort((a, b) => {
                const orderA = orderDict[a.id] !== undefined ? orderDict[a.id] : 999;
                const orderB = orderDict[b.id] !== undefined ? orderDict[b.id] : 999;
                return orderA - orderB;
            });
            
            this.state.apps = apps;
            this.state.config = config;
            this.state.tempConfig = { ...config };
        });

        this.onToggleCustomizerEvent = () => this.toggleCustomizer();

        this._tickClock = () => {
            const now = new Date();
            this.state.currentTime = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            this.state.currentDate = now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
        };

        onMounted(() => {
            document.addEventListener("keydown", this.onGlobalKeydown);
            document.addEventListener("hoss:toggle-customizer", this.onToggleCustomizerEvent);
            document.body.classList.add("hoss_dashboard_active");
            document.body.classList.add(`hoss_bg_behavior_${this.state.config.background_behavior || 'fixed'}`);
            if (this.state.customizerOpen) {
                document.body.classList.add("hoss_customizer_open");
            }
            this.env.bus.trigger("HOME-MENU:TOGGLED");
            this.applyStyles(this.state.config);

            // Initial tick, then sync to next minute boundary to avoid drift
            this._tickClock();
            const now = new Date();
            const msToNextMinute = (60 - now.getSeconds()) * 1000 - now.getMilliseconds();
            this._clockSyncTimeout = setTimeout(() => {
                this._tickClock();
                this._clockInterval = setInterval(this._tickClock, 60000);
            }, msToNextMinute);
        });

        onWillUnmount(() => {
            document.removeEventListener("keydown", this.onGlobalKeydown);
            document.removeEventListener("hoss:toggle-customizer", this.onToggleCustomizerEvent);
            document.body.classList.remove("hoss_dashboard_active");
            document.body.classList.remove("hoss_customizer_open");
            document.body.classList.remove("hoss_bg_behavior_fixed");
            document.body.classList.remove("hoss_bg_behavior_scroll");
            document.body.style.removeProperty("--hoss-bg-style");
            document.body.style.removeProperty("--hoss-bg-saturation");
            document.body.style.removeProperty("--hoss-navbar-text-color");
            this.env.bus.trigger("HOME-MENU:TOGGLED");

            clearTimeout(this._clockSyncTimeout);
            clearInterval(this._clockInterval);
            // Invalidate shared cache so next dashboard mount fetches fresh data
            cachedConfig = null;
            originalOrderDict = null;
        });

        onPatched(() => {
            const activeConfig = this.state.customizerOpen ? this.state.tempConfig : this.state.config;
            document.body.classList.remove("hoss_bg_behavior_fixed", "hoss_bg_behavior_scroll");
            document.body.classList.add(`hoss_bg_behavior_${activeConfig.background_behavior || 'fixed'}`);
            if (this.state.customizerOpen) {
                document.body.classList.add("hoss_customizer_open");
            } else {
                document.body.classList.remove("hoss_customizer_open");
            }
            this.applyStyles(activeConfig);
        });
    }

    applyStyles(config) {
        const container = this.dashboardContainer.el;
        if (!container) return;
        
        let bgStyle = "";
        if (config.background_type === "color") {
            bgStyle = config.background_color || "#f1f5f9";
        } else if (config.background_type === "gradient") {
            const start = config.background_gradient_start || "#e0e7ff";
            const end = config.background_gradient_end || "#dbeafe";
            const angle = config.background_gradient_angle !== undefined ? config.background_gradient_angle : 135;
            bgStyle = `linear-gradient(${angle}deg, ${start}, ${end})`;
        } else if (config.background_type === "image") {
            const imgUrl = config.background_image_url_custom || config.background_image_url;
            bgStyle = imgUrl ? `url(${imgUrl}) center/cover no-repeat` : "linear-gradient(-45deg, #f6f8fd, #e0e7ff, #f1f5f9, #dbeafe)";
        }
        
        document.body.style.setProperty("--hoss-bg-style", bgStyle);
        document.body.style.setProperty("--hoss-bg-saturation", `${config.background_saturation !== undefined ? config.background_saturation : 100}%`);
        
        const navbarTextColor = config.clock_text_color || "#ffffff";
        document.body.style.setProperty("--hoss-navbar-text-color", navbarTextColor);
        
        container.style.setProperty("--hoss-icon-size", `${config.icon_size || 72}px`);
        
        let shapeRadius = "18px";
        if (config.icon_shape === "circle") {
            shapeRadius = "50%";
        } else if (config.icon_shape === "square") {
            shapeRadius = "4px";
        }
        container.style.setProperty("--hoss-icon-shape-radius", shapeRadius);
        
        const hexToRgba = (hex, opacity) => {
            if(/^#([A-Fa-f0-9]{3}){1,2}$/.test(hex)){
                let c = hex.substring(1).split('');
                if(c.length === 3){
                    c = [c[0], c[0], c[1], c[1], c[2], c[2]];
                }
                c = '0x' + c.join('');
                return 'rgba(' + [(c>>16)&255, (c>>8)&255, c&255].join(',') + ',' + (opacity/100) + ')';
            }
            return hex;
        };
        
        const cardColor = config.card_bg_color || "#ffffff";
        const cardOpacity = config.card_bg_opacity !== undefined ? config.card_bg_opacity : 25;
        const cardRgba = hexToRgba(cardColor, cardOpacity);
        const cardRgbaHover = hexToRgba(cardColor, Math.min(cardOpacity + 20, 100));
        
        container.style.setProperty("--hoss-card-bg", cardRgba);
        container.style.setProperty("--hoss-card-bg-hover", cardRgbaHover);
        container.style.setProperty("--hoss-card-border-radius", `${config.card_border_radius !== undefined ? config.card_border_radius : 28}px`);
        container.style.setProperty("--hoss-card-border-width", `${config.card_border_width !== undefined ? config.card_border_width : 1}px`);
        
        const borderColor = config.card_border_color || "#ffffff";
        container.style.setProperty("--hoss-card-border-color", hexToRgba(borderColor, 40));
        container.style.setProperty("--hoss-card-border-color-hover", hexToRgba(borderColor, 80));

        // Greeting layout styling variables
        container.style.setProperty("--hoss-greeting-align", config.greeting_align || "center");
        container.style.setProperty("--hoss-greeting-title-size", `${config.greeting_title_size !== undefined ? config.greeting_title_size : 48}px`);
        container.style.setProperty("--hoss-greeting-subtitle-size", `${config.greeting_subtitle_size !== undefined ? config.greeting_subtitle_size : 18}px`);
        container.style.setProperty("--hoss-greeting-spacing", `${config.greeting_spacing !== undefined ? config.greeting_spacing : 40}px`);
    }

    toggleCustomizer() {
        this.state.customizerOpen = !this.state.customizerOpen;
        this.state.saveGlobally = false;
        if (!this.state.customizerOpen) {
            // Revert changes on cancel
            this.state.tempConfig = { ...this.state.config };
        }
    }

    get activeConfig() {
        return this.state.customizerOpen ? this.state.tempConfig : this.state.config;
    }

    changeConfig(field, value) {
        const numericFields = [
            'background_gradient_angle',
            'background_saturation',
            'icon_size',
            'card_bg_opacity',
            'card_border_radius',
            'card_border_width',
            'greeting_title_size',
            'greeting_subtitle_size',
            'greeting_spacing',
            'top_padding',
            'start_hour',
            'end_hour'
        ];
        let val = value;
        if (numericFields.includes(field)) {
            val = parseInt(value) || 0;
        }

        // If card style properties are changed manually, reset theme preset selector to custom
        const cardStyleFields = [
            'show_cards',
            'card_bg_color',
            'card_bg_opacity',
            'card_border_width',
            'card_border_color',
            'card_border_radius',
            'icon_shape'
        ];
        
        let presetTheme = this.state.tempConfig.preset_theme;
        if (cardStyleFields.includes(field)) {
            presetTheme = 'custom';
        }

        this.state.tempConfig = {
            ...this.state.tempConfig,
            [field]: val,
            preset_theme: presetTheme
        };
    }

    selectLayoutMode(mode) {
        if (mode === 'classic') {
            this.state.tempConfig = {
                ...this.state.tempConfig,
                background_behavior: 'fixed',
                greeting_behavior: 'fixed'
            };
        } else if (mode === 'scroll') {
            this.state.tempConfig = {
                ...this.state.tempConfig,
                background_behavior: 'scroll',
                greeting_behavior: 'scroll'
            };
        } else if (mode === 'parallax') {
            this.state.tempConfig = {
                ...this.state.tempConfig,
                background_behavior: 'fixed',
                greeting_behavior: 'scroll'
            };
        }
    }

    selectPresetTheme(theme) {
        let updates = { preset_theme: theme };
        if (theme === 'glass') {
            updates = {
                ...updates,
                show_cards: true,
                card_bg_color: "#ffffff",
                card_bg_opacity: 25,
                card_border_width: 1,
                card_border_color: "#ffffff",
                card_border_radius: 28,
                icon_shape: "rounded"
            };
        } else if (theme === 'minimal') {
            updates = {
                ...updates,
                show_cards: false,
                card_border_width: 0,
                card_border_radius: 0,
                icon_shape: "circle"
            };
        } else if (theme === 'flat') {
            updates = {
                ...updates,
                show_cards: true,
                card_bg_color: "#ffffff",
                card_bg_opacity: 95,
                card_border_width: 1,
                card_border_color: "#e2e8f0",
                card_border_radius: 12,
                icon_shape: "square"
            };
        }
        this.state.tempConfig = {
            ...this.state.tempConfig,
            ...updates
        };
    }

    async saveCustomizerSettings() {
        const toSave = { ...this.state.tempConfig };
        // Strip read-only / non-writable keys
        const NON_SAVE_KEYS = [
            'id', 'background_image_url', 'is_admin', 'allow_user_customization',
            'can_customize', 'greeting_html', 'systems', 'is_custom', 'name'
        ];
        NON_SAVE_KEYS.forEach(k => delete toSave[k]);

        await this.orm.call("hoss.dashboard.config", "save_config", [toSave, this.state.saveGlobally]);
        
        // Re-fetch server-confirmed fresh config (avoids stale state after save)
        const localHour = new Date().getHours();
        const freshConfig = await this.orm.call("hoss.dashboard.config", "get_config", [localHour]);
        if (freshConfig.greeting_html) {
            freshConfig.greeting_html = markup(freshConfig.greeting_html);
        }
        cachedConfig = freshConfig;
        this.state.config = freshConfig;
        this.state.tempConfig = { ...freshConfig };
        this.state.customizerOpen = false;

        // Notify systray components (clock, customizer btn) that config changed
        document.dispatchEvent(new CustomEvent('hoss:config-updated'));
    }

    cancelCustomizerSettings() {
        this.state.tempConfig = { ...this.state.config };
        this.state.customizerOpen = false;
    }

    async openBackendConfig() {
        const action = await this.orm.call("hoss.dashboard.config", "get_config_action", []);
        this.actionService.doAction(action);
    }

    async onSystemChange(ev) {
        const value = ev.target.value;
        if (value === "custom") return;

        const systemId = parseInt(value);
        if (isNaN(systemId)) return;

        if (this.state.tempConfig.is_custom) {
            const confirmChange = confirm("اختيارك لنظام جاهز سيؤدي إلى حذف إعداداتك المخصصة السابقة. هل تريد المتابعة؟");
            if (!confirmChange) {
                ev.target.value = "custom";
                return;
            }
        }

        const success = await this.orm.call("hoss.dashboard.config", "set_user_system", [systemId, this.state.tempConfig.is_custom]);
        if (success) {
            const localHour = new Date().getHours();
            const config = await this.orm.call("hoss.dashboard.config", "get_config", [localHour]);
            cachedConfig = config;
            if (config.greeting_html) {
                config.greeting_html = markup(config.greeting_html);
            }
            this.state.config = config;
            this.state.tempConfig = { ...config };

            let orderDict = {};
            try {
                orderDict = JSON.parse(config.app_order || "{}");
            } catch (e) {
                orderDict = {};
            }
            const apps = [...this.state.apps];
            apps.sort((a, b) => {
                const orderA = orderDict[a.id] !== undefined ? orderDict[a.id] : 999;
                const orderB = orderDict[b.id] !== undefined ? orderDict[b.id] : 999;
                return orderA - orderB;
            });
            this.state.apps = apps;
            this.applyStyles(config);
        }
    }

    onGlobalKeydown(ev) {
        // Ignore if user is already typing in an input/textarea
        if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA" || ev.target.isContentEditable) return;
        // Ignore modifier keys
        if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
        // Ignore special keys (Enter, Esc, arrows, etc.) unless it's a character
        if (ev.key.length !== 1 && ev.key !== "Backspace") return;
        
        ev.preventDefault();
        const searchValue = ev.key.length === 1 ? `/${ev.key}` : `/`;
        this.command.openMainPalette({ searchValue });
    }

    openApp(app) {
        this.menuService.selectMenu(app);
    }

    get filteredApps() {
        return this.state.apps;
    }
    
    onDragStart(app, ev) {
        if (!this.state.config.is_admin || this.state.config.lock_app_positions) return;
        ev.dataTransfer.effectAllowed = "move";
        this.draggedAppTemp = app;
        setTimeout(() => {
            if (this.draggedAppTemp) {
                this.state.draggedApp = this.draggedAppTemp;
            }
        }, 0);
    }

    async onDragEnd(app, ev) {
        this.draggedAppTemp = null;
        this.state.draggedApp = null;
        if (!this.state.config.is_admin || this.state.config.lock_app_positions) {
            return;
        }

        const apps = this.state.apps;
        const newOrderDict = {};
        apps.forEach((a, idx) => { newOrderDict[a.id] = idx; });

        // Skip DB write if order did not actually change
        const prevStr = JSON.stringify(originalOrderDict || {});
        const newStr = JSON.stringify(newOrderDict);
        if (prevStr === newStr) return;

        originalOrderDict = { ...newOrderDict };
        await this.orm.call("hoss.dashboard.config", "save_dashboard_app_order", [JSON.stringify(newOrderDict)]);
    }

    onDragEnter(targetApp, ev) {
        if (!this.state.config.is_admin || this.state.config.lock_app_positions) return;
        const draggedApp = this.state.draggedApp || this.draggedAppTemp;
        if (!draggedApp || draggedApp.id === targetApp.id) return;

        const apps = [...this.state.apps];
        const draggedIdx = apps.findIndex(a => a.id === draggedApp.id);
        const targetIdx = apps.findIndex(a => a.id === targetApp.id);

        if (draggedIdx !== -1 && targetIdx !== -1 && draggedIdx !== targetIdx) {
            apps.splice(draggedIdx, 1);
            apps.splice(targetIdx, 0, draggedApp);
            this.state.apps = apps;
        }
    }

    onDragOver(ev) {
        if (!this.state.config.is_admin || this.state.config.lock_app_positions) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
    }

    onDrop(targetApp, ev) {
        ev.preventDefault();
    }
}

// ─── Navbar Clock Systray ────────────────────────────────────────────────────
export class HossClockSystray extends Component {
    static template = xml`
        <div t-if="state.isDashboardActive and state.showClock"
             class="hoss_nav_clock"
             t-att-style="state.clockStyle">
            <div class="hoss_nav_clock_inner">
                <span class="hoss_nav_clock_time" t-esc="state.currentTime"/>
                <span class="hoss_nav_clock_date" t-esc="state.currentDate"/>
            </div>
        </div>
    `;

    setup() {
        this.state = useState({
            isDashboardActive: this._isDashboardActive(),
            showClock: this._shouldShow(),
            clockStyle: this._computeClockStyle(),
            currentTime: "",
            currentDate: "",
        });

        useBus(this.env.bus, "HOME-MENU:TOGGLED", () => {
            this.state.isDashboardActive = this._isDashboardActive();
            this.state.showClock = this._shouldShow();
            this.state.clockStyle = this._computeClockStyle();
        });

        // React immediately when user saves customizer settings
        this._onConfigUpdated = () => {
            this.state.showClock = this._shouldShow();
            this.state.clockStyle = this._computeClockStyle();
        };

        const tick = () => {
            const now = new Date();
            this.state.currentTime = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            this.state.currentDate = now.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
        };

        onMounted(() => {
            tick();
            document.addEventListener('hoss:config-updated', this._onConfigUpdated);
            const now = new Date();
            const msToNext = (60 - now.getSeconds()) * 1000 - now.getMilliseconds();
            this._syncTimeout = setTimeout(() => {
                tick();
                this._interval = setInterval(tick, 60000);
            }, msToNext);
        });

        onWillUnmount(() => {
            document.removeEventListener('hoss:config-updated', this._onConfigUpdated);
            clearTimeout(this._syncTimeout);
            clearInterval(this._interval);
        });
    }

    _isDashboardActive() {
        return document.body.classList.contains("hoss_dashboard_active");
    }

    _shouldShow() {
        if (!this._isDashboardActive() || !cachedConfig) return false;
        return cachedConfig.show_clock !== false;
    }

    /**
     * Converts a hex color (#rrggbb) to "r, g, b" string for use in rgba().
     */
    _hexToRgb(hex) {
        hex = hex || '#ffffff';
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        // Use isNaN to prevent turning 0 (black) into 255 (white)
        return `${isNaN(r) ? 255 : r}, ${isNaN(g) ? 255 : g}, ${isNaN(b) ? 255 : b}`;
    }

    _computeClockStyle() {
        if (!cachedConfig) return '';
        const c = cachedConfig;

        const txtRgb = this._hexToRgb(c.clock_text_color || '#ffffff');
        const bgRgb  = this._hexToRgb(c.clock_bg_color || '#ffffff');
        const bdrRgb = this._hexToRgb(c.clock_border_color || '#ffffff');

        const bgOp  = ((c.clock_bg_opacity ?? 13) / 100).toFixed(2);
        const bdrW  = (c.clock_border_width ?? 1) + 'px';

        const shadow = c.clock_shadow !== false
            ? `0 4px 18px rgba(0, 0, 0, 0.18), inset 0 1px 2px rgba(255, 255, 255, 0.25)`
            : 'none';
        
        const textShadow = c.clock_text_shadow !== false
            ? `0 1px 6px rgba(${txtRgb}, 0.5)`
            : 'none';

        return [
            `--hoss-clock-text: rgba(${txtRgb}, 0.95)`,
            `--hoss-clock-date: rgba(${txtRgb}, 0.68)`,
            `--hoss-clock-bg: rgba(${bgRgb}, ${bgOp})`,
            `--hoss-clock-border-color: rgba(${bdrRgb}, 0.65)`,
            `--hoss-clock-border-width: ${bdrW}`,
            `--hoss-clock-shadow: ${shadow}`,
            `--hoss-clock-text-shadow: ${textShadow}`,
        ].join('; ');
    }
}

export class HossCustomizerSystray extends Component {
    static template = xml`
        <button t-if="state.isDashboardActive and state.canCustomize"
                class="hoss_nav_customizer_btn border-0 bg-transparent px-3"
                title="Customize Dashboard"
                t-on-click="toggleCustomizer"
                style="display: flex; align-items: center; justify-content: center; height: 100%; cursor: pointer;">
            <i class="fa fa-paint-brush fs-5"></i>
        </button>
    `;

    setup() {
        this.state = useState({
            isDashboardActive: this.isDashboardActive,
            canCustomize: this.canCustomize,
        });
        useBus(this.env.bus, "HOME-MENU:TOGGLED", () => {
            this.state.isDashboardActive = this.isDashboardActive;
            this.state.canCustomize = this.canCustomize;
        });
    }

    get isDashboardActive() {
        return document.body.classList.contains("hoss_dashboard_active");
    }

    get canCustomize() {
        return cachedConfig && cachedConfig.can_customize;
    }

    toggleCustomizer() {
        document.dispatchEvent(new CustomEvent('hoss:toggle-customizer'));
    }
}

registry.category("systray").add("hoss_clock",      { Component: HossClockSystray },      { sequence: 1000 });
registry.category("systray").add("hoss_customizer", { Component: HossCustomizerSystray }, { sequence: 10 });

registry.category("actions").add("hoss_dashboard", hossDashboard);
registry.category("actions").add("menu", hossDashboard, { force: true });

const originalRegistryAdd = Registry.prototype.add;
Registry.prototype.add = function (key, value, options = {}) {
    if ((this.name === "actions" || this === registry.category("actions")) && key === "menu") {
        options.force = true;
        value = hossDashboard;
    }
    return originalRegistryAdd.call(this, key, value, options);
};
