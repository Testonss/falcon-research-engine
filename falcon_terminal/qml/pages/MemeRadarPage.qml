import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../components"

ColumnLayout {
    id: root
    spacing: FalconTheme.s16

    // Header
    RowLayout {
        Layout.fillWidth: true
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2
            Text {
                text: "Meme Coin Radar"
                color: FalconTheme.textPrimary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.pageTitle
                font.bold: true
            }
            Text {
                text: "Discover and track fast-moving tokens on Solana and Axiom-compatible feeds"
                color: FalconTheme.textSecondary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.body
            }
        }
        Badge { text: "PRESETS — SOON"; fg: FalconTheme.textMuted; opacity: 0.7 }
        Rectangle {
            Layout.preferredWidth: 90; Layout.preferredHeight: 30
            radius: FalconTheme.r6
            color: "transparent"
            border.color: FalconTheme.accentGold
            Text { anchors.centerIn: parent; text: "Reset"; color: FalconTheme.accentGold; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; font.bold: true }
            MouseArea { anchors.fill: parent; onClicked: { venueBox.currentIndex = 0; chainBox.currentIndex = 0; } }
        }
    }

    // Filter row (visual contract; advanced filters need the Radar backend)
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s12
        ColumnLayout {
            spacing: 2
            Text { text: "EXCHANGE / VENUE"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
            ComboBox {
                id: venueBox
                Layout.preferredWidth: 170
                model: ["All venues"].concat(ctrl.assetDexes)
                font.family: FalconTheme.fontFamily
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 }
            }
        }
        ColumnLayout {
            spacing: 2
            Text { text: "CHAIN"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
            ComboBox {
                id: chainBox
                Layout.preferredWidth: 120
                model: ["Solana"]
                font.family: FalconTheme.fontFamily
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 }
            }
        }
        ColumnLayout {
            spacing: 2
            Text { text: "MIN LIQUIDITY"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
            ComboBox {
                Layout.preferredWidth: 110
                enabled: false
                model: ["$50K"]
                font.family: FalconTheme.fontFamily
                opacity: 0.6
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderSubtle; radius: FalconTheme.r6 }
            }
        }
        ColumnLayout {
            spacing: 2
            Text { text: "MIN AGE"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
            ComboBox {
                Layout.preferredWidth: 100
                enabled: false
                model: ["Any"]
                font.family: FalconTheme.fontFamily
                opacity: 0.6
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderSubtle; radius: FalconTheme.r6 }
            }
        }
        ColumnLayout {
            spacing: 2
            Text { text: "SCAM FILTERS"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
            Switch { enabled: false; checked: true; opacity: 0.6 }
        }
        ColumnLayout {
            spacing: 2
            Text { text: "HOLDER CAP"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
            ComboBox {
                Layout.preferredWidth: 100
                enabled: false
                model: ["< 30%"]
                font.family: FalconTheme.fontFamily
                opacity: 0.6
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderSubtle; radius: FalconTheme.r6 }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredHeight: 420
        spacing: FalconTheme.s16

        // Token table (real Solana rows only)
        Panel {
            objectName: "tokenTablePanel"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 3
            ListView {
                id: tokens
                anchors.fill: parent
                model: assets
                clip: true
                delegate: Rectangle {
                    visible: provider === "solana"
                        && (venueBox.currentText === "All venues" || venueBox.currentText === "" || dex === venueBox.currentText)
                    height: (visible ? 52 : 0)
                    width: tokens.width
                    color: selected.key === key ? FalconTheme.bgSelected : (index % 2 ? FalconTheme.bgPanel : FalconTheme.bgSecondary)
                    border.color: selected.key === key ? FalconTheme.accentGold : "transparent"
                    border.width: 1
                    MouseArea {
                        anchors.fill: parent
                        onClicked: selected.select(key)
                    }
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: FalconTheme.s12
                        anchors.rightMargin: FalconTheme.s12
                        spacing: FalconTheme.s12
                        Text {
                            text: favorite ? "★" : "☆"
                            color: favorite ? FalconTheme.accentGold : FalconTheme.textMuted
                            font.pixelSize: 14
                            Layout.preferredWidth: 18
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: function(mouse) {
                                    mouse.accepted = true
                                    ctrl.toggleFavorite(key)
                                }
                            }
                        }
                        Text { text: symbol; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true; Layout.preferredWidth: 90 }
                        Text { text: "SOL"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 40 }
                        Text { text: priceText; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 110; horizontalAlignment: Text.AlignRight }
                        Text { text: "—"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 60; horizontalAlignment: Text.AlignRight }
                        Text { text: "—"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 60; horizontalAlignment: Text.AlignRight }
                        Text { text: feed; color: connected ? FalconTheme.positive : FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 120 }
                        Text { text: "—"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 50; horizontalAlignment: Text.AlignHCenter }
                        Text { text: "—"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; Layout.preferredWidth: 70; horizontalAlignment: Text.AlignHCenter }
                        Text { text: "UNRANKED"; color: FalconTheme.warning; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro; font.bold: true; Layout.preferredWidth: 80; horizontalAlignment: Text.AlignHCenter }
                    }
                }
                header: Rectangle {
                    width: tokens.width; height: 30; color: "transparent"
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: FalconTheme.s12
                        anchors.rightMargin: FalconTheme.s12
                        spacing: FalconTheme.s12
                        Text { Layout.preferredWidth: 18; text: "" }
                        Text { Layout.preferredWidth: 90; text: "TOKEN"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                        Text { Layout.preferredWidth: 40; text: "CHAIN"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                        Text { Layout.preferredWidth: 110; text: "PRICE"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignRight }
                        Text { Layout.preferredWidth: 60; text: "5M %"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignRight }
                        Text { Layout.preferredWidth: 60; text: "1H %"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignRight }
                        Text { Layout.preferredWidth: 120; text: "FEED"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                        Text { Layout.preferredWidth: 50; text: "RISK"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignHCenter }
                        Text { Layout.preferredWidth: 70; text: "FALCON"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignHCenter }
                        Text { Layout.preferredWidth: 80; text: "STATUS"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignHCenter }
                    }
                }
                footer: Text {
                    width: tokens.width
                    text: "Single-asset feed — full discovery arrives with Falcon Radar. Unsupported columns render — by design."
                    color: FalconTheme.textMuted
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    leftPadding: FalconTheme.s12
                    topPadding: FalconTheme.s8
                }
            }
        }

        // Selected token panel
        Panel {
            objectName: "tokenPanel"
            title: "Selected Token"
            Layout.preferredWidth: ApplicationWindow.window ? ApplicationWindow.window.sidePanelWidth : 360
            Layout.minimumWidth: 280
            Layout.fillHeight: true
            Layout.fillWidth: false
            ColumnLayout {
                anchors.fill: parent
                spacing: FalconTheme.s8
                Text { text: selected.symbol; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.assetTitle; font.bold: true }
                Text { text: selected.key; color: FalconTheme.textMuted; font.family: "Consolas"; font.pixelSize: FalconTheme.micro }
                Text { text: selected.price; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.displayPrice; font.bold: true }
                Sparkline { values: selected.history; up: true; fontFamily: FalconTheme.fontFamily; Layout.fillWidth: true; Layout.preferredHeight: 90 }
                GridLayout {
                    columns: 3
                    Layout.fillWidth: true
                    columnSpacing: FalconTheme.s8
                    rowSpacing: FalconTheme.s8
                    MetricBlock { label: "Liquidity"; value: selected.liquidity; Layout.fillWidth: true }
                    MetricBlock { label: "Market Cap"; value: selected.marketCap; Layout.fillWidth: true }
                    MetricBlock { label: "Volume"; value: selected.volume; Layout.fillWidth: true }
                    MetricBlock { label: "Buys / Sells"; value: selected.buys + " / " + selected.sells; Layout.fillWidth: true }
                    MetricBlock { label: "DEX"; value: selected.dex; Layout.fillWidth: true }
                    MetricBlock { label: "Pool"; value: selected.pool.length > 14 ? selected.pool.slice(0, 14) + "…" : selected.pool; Layout.fillWidth: true }
                }
                Text { text: "HOLDER CONCENTRATION"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
                Text { text: "— (Radar backend required)"; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Text { text: "RISK FLAGS"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
                Text { text: "No risk engine connected"; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                Badge { text: "FALCON VERDICT: UNRANKED"; fg: FalconTheme.warning; bg: "#2a2118"; line: FalconTheme.accentDim }
            }
        }
    }

    // Bottom row
    RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: ApplicationWindow.window ? ApplicationWindow.window.bottomPanelHeight + 10 : 140
        Layout.minimumHeight: 100
        spacing: FalconTheme.s16
        Panel {
            objectName: "whyPanel"
            title: "Why it passed"
            Layout.fillWidth: true
            Layout.fillHeight: true
            EmptyState { anchors.fill: parent; title: ""; body: "Analysis engine not active" }
        }
        Panel {
            objectName: "insightPanel"
            title: "AI Insight"
            Layout.fillWidth: true
            Layout.fillHeight: true
            EmptyState { anchors.fill: parent; title: ""; body: "Waiting for analysis engine" }
        }
    }
}
