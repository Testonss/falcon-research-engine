import QtQuick

Canvas {
    id: root
    // candles: list of [iso, o, h, l, c, v, closed]; history: list of prices
    property var candles: []
    property var history: []
    property string mode: "candles" // or "area"
    property string fontFamily: "sans-serif"
    property color seriesColor: FalconTheme.accentGold
    onCandlesChanged: requestPaint()
    onHistoryChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    function _series() {
        if (mode === "candles" && candles && candles.length >= 2)
            return { kind: "candles" };
        if (history && history.length >= 2)
            return { kind: "area" };
        return null;
    }

    onPaint: {
        var ctx = getContext("2d");
        ctx.clearRect(0, 0, width, height);
        var left = 8, right = 64, top = 8, bottom = 22;
        var W = width - left - right, H = height - top - bottom;
        var s = _series();
        ctx.font = "10px '" + fontFamily + "'";
        if (!s) {
            ctx.fillStyle = FalconTheme.textMuted;
            ctx.fillText("waiting for market data", left + 8, top + 20);
            return;
        }
        var lo = Infinity, hi = -Infinity, closes = [], i;
        if (s.kind === "candles") {
            for (i = 0; i < candles.length; i++) {
                lo = Math.min(lo, candles[i][3]); hi = Math.max(hi, candles[i][2]);
                closes.push(candles[i][4]);
            }
        } else {
            for (i = 0; i < history.length; i++) {
                lo = Math.min(lo, history[i]); hi = Math.max(hi, history[i]);
            }
        }
        if (!(hi > lo)) { hi = lo + (Math.abs(lo) || 1.0); }
        var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
        function X(i, n) { return left + W * (n === 1 ? 0.5 : i / (n - 1)); }
        function Y(v) { return top + H * (1 - (v - lo) / (hi - lo)); }

        // grid + y labels
        ctx.strokeStyle = FalconTheme.borderSubtle;
        ctx.fillStyle = FalconTheme.textMuted;
        ctx.lineWidth = 1;
        for (var g = 0; g <= 4; g++) {
            var gy = top + H * g / 4;
            ctx.beginPath(); ctx.moveTo(left, gy); ctx.lineTo(left + W, gy); ctx.stroke();
            var gv = hi - (hi - lo) * g / 4;
            ctx.fillText(gv.toLocaleString("en-US", {maximumFractionDigits: 2}), left + W + 6, gy + 3);
        }
        // x labels (time from iso strings when available)
        ctx.fillStyle = FalconTheme.textMuted;
        function labelFor(idx) {
            if (s.kind === "candles" && candles[idx] && candles[idx][0]) {
                var d = new Date(candles[idx][0]);
                if (!isNaN(d)) {
                    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
                }
            }
            return "";
        }
        var n = s.kind === "candles" ? candles.length : history.length;
        for (var t = 0; t < 5; t++) {
            var ti = Math.round((n - 1) * t / 4);
            ctx.fillText(labelFor(ti), X(ti, n) - 12, height - 6);
        }

        if (s.kind === "candles") {
            var bw = Math.max(2, W / n * 0.6);
            for (i = 0; i < n; i++) {
                var c = candles[i];
                var forming = !c[6];
                var bull = c[4] >= c[1];
                var col = bull ? FalconTheme.positive : FalconTheme.negative;
                ctx.strokeStyle = col; ctx.fillStyle = col;
                ctx.globalAlpha = forming ? 0.45 : 1.0;
                var x = X(i, n);
                ctx.beginPath(); ctx.moveTo(x, Y(c[2])); ctx.lineTo(x, Y(c[3])); ctx.stroke();
                var yo = Y(c[1]), yc = Y(c[4]);
                ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(2, Math.abs(yc - yo)));
                ctx.globalAlpha = 1.0;
            }
            var last = closes[closes.length - 1];
            ctx.fillStyle = FalconTheme.accentGold;
            var ly = Y(last);
            ctx.fillRect(left + W - 2, top, 2, H);
            var tag = last.toLocaleString("en-US", {maximumFractionDigits: 2});
            ctx.fillRect(left + W - 2, ly - 9, right - 4, 18);
            ctx.fillStyle = "#0a0d13";
            ctx.fillText(tag, left + W + 2, ly + 3);
        } else {
            var grad = ctx.createLinearGradient(0, top, 0, top + H);
            grad.addColorStop(0, "rgba(242,169,59,0.35)");
            grad.addColorStop(1, "rgba(242,169,59,0.0)");
            ctx.beginPath();
            for (i = 0; i < history.length; i++) {
                var px = X(i, history.length), py = Y(history[i]);
                if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
            }
            ctx.strokeStyle = seriesColor; ctx.lineWidth = 1.8; ctx.stroke();
            ctx.lineTo(X(history.length - 1, history.length), top + H);
            ctx.lineTo(X(0, history.length), top + H);
            ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
            var lv = history[history.length - 1];
            ctx.fillStyle = FalconTheme.accentGold;
            ctx.fillRect(left + W - 2, Y(lv) - 9, right - 4, 18);
            ctx.fillStyle = "#0a0d13";
            ctx.fillText(lv.toLocaleString("en-US", {maximumFractionDigits: 2}), left + W + 2, Y(lv) + 3);
        }
    }
}
