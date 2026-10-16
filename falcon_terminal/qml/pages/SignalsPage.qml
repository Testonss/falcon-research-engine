import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../components"

ColumnLayout {
    id: root
    spacing: FalconTheme.s16

    // Asset strip
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s16
        Text {
            text: selected.symbol
            color: FalconTheme.textPrimary
            font.family: FalconTheme.fontFamily
            font.pixelSize: 24
            font.bold: true
        }
        Text {
            text: selected.price + " " + selected.quote
            color: FalconTheme.textPrimary
            font.family: FalconTheme.fontFamily
            font.pixelSize: 24
            font.bold: true
        }
        Text {
            visible: selected.hasChange
            text: selected.change
            color: selected.changeUp ? FalconTheme.positive : FalconTheme.negative
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.body
            font.bold: true
        }
        Item { Layout.fillWidth: true }
        MetricBlock { label: "24H High"; value: "—" }
        MetricBlock { label: "24H Low"; value: "—" }
        MetricBlock { label: "24H Vol"; value: "—" }
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredHeight: 420
        spacing: FalconTheme.s16

        // Main chart panel
        Panel {
            objectName: "chartPanel"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 3
            ColumnLayout {
                anchors.fill: parent
                spacing: FalconTheme.s8
                RowLayout {
                    Layout.fillWidth: true
                    TimePills {
                        active: selected.timeframe
                        supported: selected.supportedTimeframes
                        onPick: function(tf) { ctrl.setTimeframe(tf) }
                    }
                    Item { Layout.fillWidth: true }
                    Text { text: selected.freshness; color: selected.isStale ? FalconTheme.warning : FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: selected.candleState; color: FalconTheme.warning; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                }
                Text {
                    visible: selected.chartLoading || selected.chartNote !== ""
                    text: selected.chartLoading ? ("Loading " + selected.timeframe + "…") : selected.chartNote
                    color: FalconTheme.textSecondary
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                }
                PriceChart {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    mode: "candles"
                    fontFamily: FalconTheme.fontFamily
                    history: selected.history
                    candles: selected.candles
                    opacity: selected.chartLoading ? 0.45 : 1.0
                }
                Text {
                    text: selected.candleLine
                    color: FalconTheme.textSecondary
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                }
            }
        }

        // Trade setup panel
        Panel {
            objectName: "tradeSetupPanel"
            title: "Trade Setup"
            Layout.preferredWidth: ApplicationWindow.window ? ApplicationWindow.window.sidePanelWidth : 340
            Layout.minimumWidth: 280
            Layout.fillHeight: true
            Layout.fillWidth: false
            ColumnLayout {
                anchors.fill: parent
                spacing: FalconTheme.s8
                Text { text: "Direction"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: 20; font.bold: true }
                GridLayout {
                    columns: 2
                    Layout.fillWidth: true
                    columnSpacing: FalconTheme.s12
                    rowSpacing: FalconTheme.s8
                    Text { text: "Confidence"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                    Text { text: "Strategy Family"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                    Text { text: "Market Regime"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                    Text { text: "Expected R:R"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                    Text { text: "Invalidation"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                    Text { text: "Time Horizon"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                }
                Badge { text: "WAITING FOR STRATEGY ENGINE"; fg: FalconTheme.warning; bg: "#2a2118"; line: FalconTheme.accentDim }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: FalconTheme.s8
                    Rectangle {
                        id: watchBtn
                        property bool hovered: watchMouse.containsMouse
                        property bool pressed: watchMouse.pressed
                        Layout.preferredWidth: 110
                        Layout.preferredHeight: 30
                        radius: FalconTheme.r6
                        color: ctrl.watchedKeys.indexOf(selected.key) >= 0 ? "#2a2118" : (watchBtn.pressed ? FalconTheme.bgSelected : (watchBtn.hovered ? FalconTheme.bgHover : "transparent"))
                        border.color: ctrl.watchedKeys.indexOf(selected.key) >= 0 ? FalconTheme.accentGold : (watchBtn.hovered ? FalconTheme.borderActive : FalconTheme.borderDefault)
                        Text {
                            anchors.centerIn: parent
                            text: ctrl.watchedKeys.indexOf(selected.key) >= 0 ? "WATCHING ✓" : "WATCH"
                            color: ctrl.watchedKeys.indexOf(selected.key) >= 0 ? FalconTheme.accentGold : FalconTheme.textSecondary
                            font.family: FalconTheme.fontFamily
                            font.pixelSize: FalconTheme.small
                            font.bold: true
                        }
                        MouseArea {
                            id: watchMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: ctrl.toggleWatch(selected.key)
                        }
                    }
                    Badge { text: "PAPER — DISABLED"; fg: FalconTheme.textMuted; opacity: 0.6 }
                    Badge { text: "MANUAL ONLY"; fg: FalconTheme.info }
                }
                Text {
                    text: "Advisory / Simulation Only — not automated execution."
                    color: FalconTheme.textMuted
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.micro
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                Rectangle { Layout.fillWidth: true; height: 1; color: FalconTheme.borderSubtle }
                Text {
                    text: selected.provider === "mt5" ? ("MT5 DEMO eligible · " + mt5account.bannerText) : "MT5 DEMO: select an MT5 instrument to enable demo orders."
                    color: selected.provider === "mt5" ? FalconTheme.positive : FalconTheme.textMuted
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 34
                    radius: FalconTheme.r6
                    color: (selected.provider === "mt5" && mt5account.executionEnabled) ? "#12382a" : "transparent"
                    border.color: (selected.provider === "mt5" && mt5account.executionEnabled) ? FalconTheme.positive : FalconTheme.borderDefault
                    opacity: (selected.provider === "mt5" && mt5account.executionEnabled) ? 1.0 : 0.5
                    Text { anchors.centerIn: parent; text: "OPEN MT5 DEMO POSITION"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; font.bold: true }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        enabled: selected.provider === "mt5" && mt5account.executionEnabled
                        onClicked: nav.go("mt5")
                    }
                }
                Text {
                    visible: mt5account.state === "CONNECTED_REAL_BLOCKED"
                    text: "MT5 REAL ACCOUNT DETECTED — EXECUTION DISABLED"
                    color: FalconTheme.negative
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    font.bold: true
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }
        }
    }

    // Bottom row
    RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: ApplicationWindow.window ? ApplicationWindow.window.bottomPanelHeight + 20 : 170
        Layout.minimumHeight: 120
        spacing: FalconTheme.s16
        Panel {
            objectName: "queuePanel"
            title: "Signal Queue"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 2
            EmptyState { anchors.fill: parent; title: ""; body: "No signals queued — the strategy engine is not connected." }
        }
        Panel {
            objectName: "aiNotesPanel"
            title: "AI / Quant Notes"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 2
            EmptyState { anchors.fill: parent; title: ""; body: "Waiting for analysis engine" }
        }
        Panel {
            objectName: "perfPanel"
            title: "Signal Performance"
            Layout.preferredWidth: 300
            Layout.fillHeight: true
            Layout.fillWidth: false
            GridLayout {
                anchors.fill: parent
                columns: 2
                columnSpacing: FalconTheme.s12
                rowSpacing: FalconTheme.s8
                Text { text: "Win Rate"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                Text { text: "Expectancy"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                Text { text: "Max Drawdown"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                Text { text: "Profit Factor"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                Text { text: "Data Feeds"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: health.connected ? "Online" : "Offline"; color: health.connected ? FalconTheme.positive : FalconTheme.negative; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
                Text { text: "Signal Engine"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: "Not connected"; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.alignment: Qt.AlignRight }
            }
        }
    }
}
