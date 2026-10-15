import QtQuick

Canvas {
    id: root
    property var values: []
    property bool up: true
    property string fontFamily: "sans-serif"
    property color lineColor: up ? FalconTheme.positive : FalconTheme.negative
    onValuesChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    onPaint: {
        var ctx = getContext("2d");
        ctx.clearRect(0, 0, width, height);
        if (!values || values.length < 2) {
            ctx.fillStyle = FalconTheme.textMuted;
            ctx.font = "10px '" + fontFamily + "'";
            ctx.fillText("—", 4, height / 2);
            return;
        }
        var lo = Math.min.apply(null, values);
        var hi = Math.max.apply(null, values);
        var span = (hi > lo) ? (hi - lo) : (Math.abs(hi) || 1.0);
        var n = values.length;
        ctx.strokeStyle = lineColor;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (var i = 0; i < n; i++) {
            var x = 2 + (width - 4) * (n === 1 ? 0.5 : i / (n - 1));
            var y = height - 3 - (height - 6) * ((values[i] - lo) / span);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
    }
}
