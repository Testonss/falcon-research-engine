import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../components"

ColumnLayout {
    id: root
    spacing: FalconTheme.s16
    property string confirmSide: "BUY"
    property string confirmError: ""
    property string actionError: ""

    // Header
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s16
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2
            Text {
                text: "MT5 Demo"
                color: FalconTheme.textPrimary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.pageTitle
                font.bold: true
            }
            Text {
                text: "Broker-executed simulated money · distinct from Falcon Paper simulator"
                color: FalconTheme.textSecondary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.body
            }
        }
        MetricCard { label: "Positions"; value: String(mt5positions.count); Layout.preferredWidth: 110 }
        MetricCard { label: "Floating P/L"; value: mt5account.profitText; Layout.preferredWidth: 140 }
    }

    // Account dashboard (actual account_info values, no secrets)
    Panel {
        objectName: "mt5AccountPanel"
        title: mt5account.bannerText
        Layout.fillWidth: true
        Layout.preferredHeight: 170
        GridLayout {
            anchors.fill: parent
            columns: 4
            columnSpacing: FalconTheme.s16
            rowSpacing: FalconTheme.s8
            Text { text: "Server"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            Text { text: mt5account.server !== "" ? mt5account.server : "—"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
            Text { text: "Mode"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            Text { text: mt5account.modeName !== "" ? mt5account.modeName : "—"; color: mt5account.isDemo ? FalconTheme.positive : FalconTheme.negative; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true }
            Text { text: "Balance"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            Text { text: mt5account.balanceText; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
            Text { text: "Equity"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            Text { text: mt5account.equityText; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
            Text { text: "Margin"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            Text { text: mt5account.marginText; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
            Text { text: "Free margin"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            Text { text: mt5account.freeMarginText; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredHeight: 380
        spacing: FalconTheme.s16

        // Positions (actual terminal positions; manual MT5 closes sync back)
        Panel {
            objectName: "mt5PositionsPanel"
            title: "MT5 DEMO positions (terminal is authoritative)"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 2
            ColumnLayout {
                anchors.fill: parent
                spacing: FalconTheme.s8
                Text {
                    visible: mt5positions.count === 0
                    text: mt5account.state === "CONNECTED_DEMO" ? "No open DEMO positions — open one from Signals or below (manual, user-directed)." : mt5account.message
                    color: FalconTheme.textSecondary
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.body
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                ListView {
                    id: posTable
                    visible: mt5positions.count > 0
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    model: mt5positions
                    clip: true
                    delegate: Rectangle {
                        width: posTable.width
                        height: 64
                        color: index % 2 ? FalconTheme.bgPanel : FalconTheme.bgSecondary
                        border.color: FalconTheme.borderSubtle
                        border.width: 1
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: FalconTheme.s12
                            anchors.rightMargin: FalconTheme.s12
                            spacing: FalconTheme.s8
                            ColumnLayout {
                                Layout.preferredWidth: 150
                                spacing: 0
                                Text { text: symbol + " " + side; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true }
                                Text { text: "#" + ticket + " · " + volumeText + " lots"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                            }
                            Text { text: entryText + " → " + currentText; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 170 }
                            Text { text: "SL " + slText + "  TP " + tpText; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 170 }
                            Text { text: profitText; color: profit >= 0 ? FalconTheme.positive : FalconTheme.negative; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true; Layout.preferredWidth: 90; horizontalAlignment: Text.AlignRight }
                            Item { Layout.fillWidth: true }
                            Rectangle {
                                Layout.preferredWidth: 150
                                Layout.preferredHeight: 30
                                radius: FalconTheme.r6
                                color: "transparent"
                                border.color: FalconTheme.borderDefault
                                visible: mt5account.executionEnabled
                                Text { anchors.centerIn: parent; text: "CLOSE POSITION"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; font.bold: true }
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    enabled: mt5account.executionEnabled && !ctrl.mt5Busy
                                    onClicked: {
                                        var err = ctrl.closeDemoPosition(ticket)
                                        root.actionError = err
                                    }
                                }
                            }
                        }
                    }
                }
                Text {
                    visible: !mt5account.executionEnabled
                    text: mt5account.state === "CONNECTED_REAL_BLOCKED" ? "MT5 REAL ACCOUNT DETECTED — EXECUTION DISABLED (no override)." : "Execution disabled — connect an MT5 DEMO account."
                    color: FalconTheme.negative
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                Text {
                    visible: root.actionError !== "" || ctrl.mt5Error !== ""
                    text: root.actionError !== "" ? root.actionError : ctrl.mt5Error
                    color: FalconTheme.negative
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }
        }

        // Manual demo trade (explicitly user-directed) + settings
        Panel {
            objectName: "mt5ManualPanel"
            title: "Manual DEMO trade (user-directed)"
            Layout.preferredWidth: 340
            Layout.fillHeight: true
            Layout.fillWidth: false
            ColumnLayout {
                anchors.fill: parent
                spacing: FalconTheme.s8
                Text { text: "Selected: " + (selected.symbol || "—") + " · " + (selected.provider || ""); color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                Text { text: "Price: " + selected.price + " " + selected.quote; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: FalconTheme.s8
                    Text { text: "Side"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    ComboBox {
                        id: sideBox
                        model: ["BUY", "SELL"]
                        Layout.fillWidth: true
                        font.family: FalconTheme.fontFamily
                    }
                    Text { text: "Vol"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    TextField {
                        id: volField
                        text: "0.01"
                        Layout.preferredWidth: 70
                        font.family: "Consolas"
                        color: FalconTheme.textPrimary
                        background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: FalconTheme.s8
                    Text { text: "SL"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    TextField { id: slField; placeholderText: "optional"; Layout.fillWidth: true; font.family: "Consolas"; color: FalconTheme.textPrimary; background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 } }
                    Text { text: "TP"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    TextField { id: tpField; placeholderText: "optional"; Layout.fillWidth: true; font.family: "Consolas"; color: FalconTheme.textPrimary; background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 } }
                }
                Text {
                    visible: selected.provider !== "mt5"
                    text: "Select an MT5 instrument in Assets to enable demo orders."
                    color: FalconTheme.warning
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 36
                    radius: FalconTheme.r6
                    color: (selected.provider === "mt5" && mt5account.executionEnabled && !ctrl.mt5Busy) ? "#12382a" : "transparent"
                    border.color: (selected.provider === "mt5" && mt5account.executionEnabled) ? FalconTheme.positive : FalconTheme.borderDefault
                    opacity: (selected.provider === "mt5" && mt5account.executionEnabled) ? 1.0 : 0.5
                    Text { anchors.centerIn: parent; text: "OPEN DEMO POSITION"; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        enabled: selected.provider === "mt5" && mt5account.executionEnabled && !ctrl.mt5Busy
                        onClicked: confirmDialog.open()
                    }
                }
                Text { text: "PAPER = Falcon simulator · MT5 DEMO = broker simulated money. P/L histories are never merged."; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            }
        }
    }

    // Demo risk settings (functional; restart terminal to reconnect)
    Panel {
        objectName: "mt5SettingsPanel"
        title: "Demo settings (credentials never stored)"
        Layout.fillWidth: true
        Layout.preferredHeight: 130
        GridLayout {
            anchors.fill: parent
            columns: 4
            columnSpacing: FalconTheme.s12
            rowSpacing: FalconTheme.s8
            Text { text: "Sync (s)"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            TextField {
                id: syncField
                text: "5.0"
                Layout.preferredWidth: 80
                font.family: "Consolas"
                color: FalconTheme.textPrimary
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 }
                onAccepted: ctrl.setMt5SyncInterval(parseFloat(text) || 5.0)
            }
            Text { text: "Default vol"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            TextField {
                id: defVolField
                text: "0.01"
                Layout.preferredWidth: 80
                font.family: "Consolas"
                color: FalconTheme.textPrimary
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 }
                onAccepted: ctrl.setMt5DefaultVolume(parseFloat(text) || 0.01)
            }
            Text { text: "Max risk/trade"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            TextField {
                id: maxRiskField
                text: "100.0"
                Layout.preferredWidth: 80
                font.family: "Consolas"
                color: FalconTheme.textPrimary
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 }
                onAccepted: ctrl.setMt5MaxRisk(parseFloat(text) || 100.0)
            }
            Text { text: "Terminal path"; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            TextField {
                id: pathField
                placeholderText: "(empty = running terminal)"
                Layout.fillWidth: true
                font.family: "Consolas"
                color: FalconTheme.textPrimary
                background: Rectangle { color: FalconTheme.bgInput; border.color: FalconTheme.borderDefault; radius: FalconTheme.r6 }
                onAccepted: ctrl.setMt5TerminalPath(text)
            }
        }
    }

    // Confirmation dialog (word DEMO obvious; human-in-the-loop)
    Dialog {
        id: confirmDialog
        title: "Confirm MT5 DEMO order"
        modal: true
        anchors.centerIn: parent
        width: 420
        ColumnLayout {
            anchors.fill: parent
            spacing: FalconTheme.s8
            Text { text: "MT5 DEMO"; color: FalconTheme.positive; font.family: FalconTheme.fontFamily; font.pixelSize: 20; font.bold: true }
            Text { text: ctrl.demoOrderPreview(selected.key); color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            Text { text: "Side: " + sideBox.currentText + " · Volume: " + volField.text + " · SL: " + (slField.text || "—") + " · TP: " + (tpField.text || "—"); color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            Text { text: "Server: " + mt5account.server + " · Mode: " + mt5account.modeName; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
            Text { visible: root.confirmError !== ""; text: root.confirmError; color: FalconTheme.negative; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: "Cancel"
                    onClicked: { root.confirmError = ""; confirmDialog.close() }
                }
                Button {
                    text: "OPEN DEMO POSITION"
                    enabled: !ctrl.mt5Busy
                    onClicked: {
                        var err = ctrl.openDemoPosition(selected.key, sideBox.currentText, parseFloat(volField.text) || 0, parseFloat(slField.text) || 0, parseFloat(tpField.text) || 0)
                        if (err === "") { root.confirmError = ""; confirmDialog.close() }
                        else { root.confirmError = err }
                    }
                }
            }
        }
    }
}
