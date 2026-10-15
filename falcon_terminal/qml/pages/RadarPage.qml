import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../components"

ColumnLayout {
    id: root
    spacing: FalconTheme.s16

    // Header + summary cards
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s16
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2
            Text {
                text: "Radar / Market"
                color: FalconTheme.textPrimary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.pageTitle
                font.bold: true
            }
            Text {
                text: "Live watchlist · Real-time market data and intelligence"
                color: FalconTheme.textSecondary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.body
            }
        }
        MetricCard { label: "Assets"; value: String(assets.count); Layout.preferredWidth: 110 }
        MetricCard { label: "Active Signals"; value: "—"; Layout.preferredWidth: 130 }
        MetricCard { label: "Exchanges"; value: ctrl.assetProviders.length > 0 ? String(ctrl.assetProviders.length) : "—"; Layout.preferredWidth: 120 }
        MetricCard { label: "Data age"; value: health.detail.split("  |  ")[0] || "—"; Layout.preferredWidth: 150 }
    }

    // Filter bar (local registry/view operation: no provider refetch)
    property bool favOnly: false
    property string filterText: ""
    // Lowercased once per selection change, not once per row per tick.
    property string providerFilter: providerBox.currentText === "All" ? "" : providerBox.currentText.toLowerCase()
    Timer {
        id: searchDebounce
        interval: 80
        repeat: false
        onTriggered: root.filterText = search.text.trim().toLowerCase()
    }
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s12
        TextField {
            id: search
            Layout.preferredWidth: 240
            placeholderText: "Search assets…"
            color: FalconTheme.textPrimary
            placeholderTextColor: FalconTheme.textMuted
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.body
            onTextChanged: searchDebounce.restart()
            background: Rectangle {
                color: FalconTheme.bgInput
                border.color: FalconTheme.borderDefault
                radius: FalconTheme.r6
            }
        }
        ComboBox {
            id: providerBox
            Layout.preferredWidth: 150
            model: ["All", "Binance", "Solana"]
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.body
            background: Rectangle {
                color: FalconTheme.bgInput
                border.color: FalconTheme.borderDefault
                radius: FalconTheme.r6
            }
        }
        ComboBox {
            Layout.preferredWidth: 150
            enabled: false
            model: ["All Sectors"]
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.body
            background: Rectangle {
                color: FalconTheme.bgInput
                border.color: FalconTheme.borderSubtle
                radius: FalconTheme.r6
                opacity: 0.6
            }
        }
        Rectangle {
            id: favToggle
            property bool hovered: favMouse.containsMouse
            property bool pressed: favMouse.pressed
            Layout.preferredWidth: 44
            Layout.preferredHeight: 34
            radius: FalconTheme.r6
            color: root.favOnly ? "#2a2118" : (favToggle.pressed ? FalconTheme.bgSelected : (favToggle.hovered ? FalconTheme.bgHover : FalconTheme.bgInput))
            border.color: root.favOnly ? FalconTheme.accentGold : (favToggle.hovered ? FalconTheme.borderActive : FalconTheme.borderDefault)
            Text {
                anchors.centerIn: parent
                text: "★"
                color: root.favOnly ? FalconTheme.accentGold : FalconTheme.textMuted
                font.pixelSize: 16
            }
            MouseArea {
                id: favMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.favOnly = !root.favOnly
            }
        }
        Rectangle {
            id: resetBtn
            property bool hovered: resetMouse.containsMouse
            property bool pressed: resetMouse.pressed
            Layout.preferredWidth: 70
            Layout.preferredHeight: 34
            radius: FalconTheme.r6
            color: resetBtn.pressed ? FalconTheme.bgSelected : (resetBtn.hovered ? FalconTheme.bgHover : "transparent")
            border.color: resetBtn.hovered ? FalconTheme.accentGold : FalconTheme.borderDefault
            Text {
                anchors.centerIn: parent
                text: "Reset"
                color: resetBtn.hovered ? FalconTheme.accentGold : FalconTheme.textSecondary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.small
                font.bold: resetBtn.hovered
            }
            MouseArea {
                id: resetMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: { search.text = ""; root.filterText = ""; providerBox.currentIndex = 0; root.favOnly = false }
            }
        }
    }

    // Main split
    RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredHeight: 480
        spacing: FalconTheme.s16

        Panel {
            objectName: "watchlistPanel"
            title: ""
            Layout.fillWidth: true
            Layout.fillHeight: true
            ListView {
                id: table
                anchors.fill: parent
                model: assets
                clip: true
                delegate: Rectangle {
                    id: row
                    // Pre-normalized searchHay (Python) + debounced filterText:
                    // single indexOf per row, no per-tick toLowerCase storm.
                    property bool match: (root.filterText === "" || searchHay.indexOf(root.filterText) >= 0)
                        && (root.providerFilter === "" || provider === root.providerFilter)
                        && (!root.favOnly || favorite)
                    visible: match
                    height: match ? 56 : 0
                    width: table.width
                    color: selected.key === key ? FalconTheme.bgSelected : (index % 2 ? FalconTheme.bgPanel : FalconTheme.bgSecondary)
                    border.color: selected.key === key ? FalconTheme.accentGold : "transparent"
                    border.width: 1
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: ctrl.selectAsset(key)
                    }
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: FalconTheme.s12
                        anchors.rightMargin: FalconTheme.s12
                        spacing: FalconTheme.s8
                        Text {
                            text: favorite ? "★" : "☆"
                            color: favorite ? FalconTheme.accentGold : FalconTheme.textMuted
                            font.pixelSize: 14
                            Layout.preferredWidth: 20
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: function(mouse) {
                                    mouse.accepted = true
                                    ctrl.toggleFavorite(key)
                                }
                            }
                        }
                        ColumnLayout {
                            Layout.preferredWidth: 130
                            spacing: 0
                            Text { text: symbol; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true }
                            Text { text: provider; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
                        }
                        Text { text: priceText; color: direction > 0 ? FalconTheme.positive : (direction < 0 ? FalconTheme.negative : FalconTheme.textPrimary); font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true; Layout.preferredWidth: 110; horizontalAlignment: Text.AlignRight }
                        Text { text: changeText; color: changeUp === true ? FalconTheme.positive : (changeUp === false ? FalconTheme.negative : FalconTheme.textMuted); font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 80; horizontalAlignment: Text.AlignRight }
                        Text { text: feed; color: connected ? (stale ? FalconTheme.warning : FalconTheme.positive) : FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 130 }
                        Text { text: scoreText; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 60; horizontalAlignment: Text.AlignHCenter }
                        Text { text: alertsText; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 60; horizontalAlignment: Text.AlignHCenter }
                        Sparkline { values: spark; up: changeUp !== false; fontFamily: FalconTheme.fontFamily; Layout.preferredWidth: 110; Layout.preferredHeight: 34 }
                    }
                }
                header: Rectangle {
                    width: table.width; height: 30; color: "transparent"
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: FalconTheme.s12
                        anchors.rightMargin: FalconTheme.s12
                        spacing: FalconTheme.s8
                        Text { Layout.preferredWidth: 20; text: "" }
                        Text { Layout.preferredWidth: 130; text: "ASSET / PAIR"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                        Text { Layout.preferredWidth: 110; text: "PRICE"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignRight }
                        Text { Layout.preferredWidth: 80; text: "24H CHANGE"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignRight }
                        Text { Layout.preferredWidth: 130; text: "FEED"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                        Text { Layout.preferredWidth: 60; text: "SCORE"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignHCenter }
                        Text { Layout.preferredWidth: 60; text: "ALERTS"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignHCenter }
                        Text { Layout.preferredWidth: 110; text: ""; }
                    }
                }
            }
        }

        // Selected asset panel
        Panel {
            objectName: "selectedPanel"
            Layout.preferredWidth: ApplicationWindow.window ? ApplicationWindow.window.sidePanelWidth : 360
            Layout.minimumWidth: 280
            Layout.fillHeight: true
            Layout.fillWidth: false
            ColumnLayout {
                anchors.fill: parent
                spacing: FalconTheme.s8
                Text { text: "Selected Asset"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: selected.symbol; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.assetTitle; font.bold: true }
                Text { text: selected.provider + " · " + selected.source; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                RowLayout {
                    spacing: FalconTheme.s8
                    Text {
                        id: bigPrice
                        text: selected.price
                        color: FalconTheme.textPrimary
                        font.family: FalconTheme.fontFamily
                        font.pixelSize: FalconTheme.displayPrice
                        font.bold: true
                    }
                    Rectangle {
                        id: flash
                        width: 8; height: 30; radius: 4
                        color: "transparent"
                        Timer {
                            id: flashTimer; interval: 450; repeat: false
                            onTriggered: flash.color = "transparent"
                        }
                        Connections {
                            target: selected
                            function onDirectionChanged() {
                                if (selected.direction > 0) flash.color = FalconTheme.positive;
                                else if (selected.direction < 0) flash.color = FalconTheme.negative;
                                else return;
                                flashTimer.restart();
                            }
                        }
                    }
                    Text { text: selected.quote; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
                    Text { text: selected.change; visible: selected.hasChange; color: selected.changeUp ? FalconTheme.positive : FalconTheme.negative; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true }
                }
                Text { text: selected.feed; color: selected.live ? FalconTheme.positive : FalconTheme.warning; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; font.bold: true }
                Text { text: selected.freshness + " · " + selected.semantics; color: selected.isStale ? FalconTheme.warning : FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                TimePills {
                    active: selected.timeframe
                    supported: selected.supportedTimeframes
                    onPick: function(tf) { ctrl.setTimeframe(tf) }
                }
                Text { text: "LIVE CHART"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
                Text {
                    visible: selected.chartLoading || selected.chartNote !== ""
                    text: selected.chartLoading ? ("Loading " + selected.timeframe + "…") : selected.chartNote
                    color: FalconTheme.textSecondary
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                }
                PriceChart { id: radarChart; Layout.fillWidth: true; Layout.preferredHeight: 170; mode: "area"; fontFamily: FalconTheme.fontFamily; history: selected.history; candles: selected.candles; opacity: selected.chartLoading ? 0.45 : 1.0 }
                GridLayout {
                    columns: 3
                    Layout.fillWidth: true
                    columnSpacing: FalconTheme.s8
                    rowSpacing: FalconTheme.s8
                    MetricBlock { label: "Bid"; value: selected.bid; Layout.fillWidth: true }
                    MetricBlock { label: "Ask"; value: selected.ask; Layout.fillWidth: true }
                    MetricBlock { label: "Spread"; value: selected.spread; Layout.fillWidth: true }
                    MetricBlock { label: "Liquidity"; value: selected.liquidity; Layout.fillWidth: true }
                    MetricBlock { label: "Volume 5m"; value: selected.volume; Layout.fillWidth: true }
                    MetricBlock { label: "24H Volume"; value: "—"; Layout.fillWidth: true }
                    MetricBlock { label: "Buys 5m"; value: selected.buys; Layout.fillWidth: true }
                    MetricBlock { label: "Sells 5m"; value: selected.sells; Layout.fillWidth: true }
                    MetricBlock { label: "Market Health"; value: selected.live ? "Live" : "—"; valueColor: selected.live ? FalconTheme.positive : FalconTheme.textMuted; Layout.fillWidth: true }
                }
                Text { text: "CURRENT CANDLE"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
                Text { text: selected.candleLine; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: selected.candleState; color: FalconTheme.warning; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            }
        }
    }

    // Bottom panels
    RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: ApplicationWindow.window ? ApplicationWindow.window.bottomPanelHeight : 150
        Layout.minimumHeight: 110
        spacing: FalconTheme.s16
        Panel {
            objectName: "liveSignalsPanel"
            title: "Live Signals"
            linkText: ""
            Layout.fillWidth: true
            Layout.fillHeight: true
            EmptyState { anchors.fill: parent; title: ""; body: "No validated signals" }
        }
        Panel {
            objectName: "marketNotesPanel"
            title: "Market Notes"
            linkText: ""
            Layout.fillWidth: true
            Layout.fillHeight: true
            EmptyState { anchors.fill: parent; title: ""; body: "Waiting for analysis engine" }
        }
    }
}
