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
                    TimePills { active: "1m"; supported: ["1m"] }
                    Item { Layout.fillWidth: true }
                    Text { text: selected.candleState; color: FalconTheme.warning; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                }
                PriceChart {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    mode: "candles"
                    fontFamily: FalconTheme.fontFamily
                    history: selected.history
                    candles: selected.candles
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
                    Badge { text: "WATCH"; fg: FalconTheme.textSecondary }
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
