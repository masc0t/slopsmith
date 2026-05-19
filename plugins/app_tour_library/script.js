(function () {
    'use strict';

    // Override the engine's default `['player']` screens list for non-has_screen
    // plugins — this tour is for the Home/Library screen, not the player.
    var PLUGIN_ID = 'app_tour_library';
    var SCREENS = ['home'];

    function _register() {
        try {
            window.slopsmithTour.register(PLUGIN_ID, { screens: SCREENS });
        } catch (e) {
            console.warn('[app_tour_library] register failed', e);
        }
    }

    if (window.slopsmithTour && typeof window.slopsmithTour.register === 'function') {
        _register();
    } else {
        // Engine inits on DOMContentLoaded after fetching /api/plugins. Plugin
        // scripts can load before or after that handler runs, so poll briefly.
        var deadline = performance.now() + 5000;
        var pollId = setInterval(function () {
            if (window.slopsmithTour && typeof window.slopsmithTour.register === 'function') {
                clearInterval(pollId);
                _register();
            } else if (performance.now() > deadline) {
                clearInterval(pollId);
            }
        }, 100);
    }

    // ── Layout nudge ──────────────────────────────────────────────────────
    // The Tuner plugin parks a FAB at `fixed bottom-5 right-5` (≈ bottom:20px,
    // right:20px, ~40px tall) on every screen. The tour engine's ? button sits
    // at bottom:12px right:12px — same corner. On the home screen they overlap
    // exactly. Scope a nudge to the home screen only so we don't move the
    // button on screens where the home tour isn't relevant anyway.
    var NUDGE_CLASS = 'app-tour-library-nudge';
    var STYLE_ID = 'app-tour-library-nudge-style';

    function _ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var s = document.createElement('style');
        s.id = STYLE_ID;
        s.textContent =
            'body.' + NUDGE_CLASS + ' .slopsmith-tour-menu-btn { bottom: 68px; }' +
            'body.' + NUDGE_CLASS + ' .slopsmith-tour-menu-popover { bottom: 112px; }' +
            'body.' + NUDGE_CLASS + ' .slopsmith-tour-prompt { bottom: 112px; }';
        document.head.appendChild(s);
    }

    function _applyNudge(screenId) {
        if (!document.body) return;
        document.body.classList.toggle(NUDGE_CLASS, screenId === 'home');
    }

    function _initNudge() {
        _ensureStyle();
        // Prime from whichever screen is already active.
        var active = document.querySelector('.screen.active');
        _applyNudge(active ? active.id : null);
        if (window.slopsmith && typeof window.slopsmith.on === 'function') {
            window.slopsmith.on('screen:changed', function (ev) {
                _applyNudge(ev && ev.detail && ev.detail.id);
            });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _initNudge, { once: true });
    } else {
        _initNudge();
    }
})();
