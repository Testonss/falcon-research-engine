import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../components"

ColumnLayout {
    id: root
    spacing: FalconTheme.s16
    property bool favOnly: false
    property bool recentOnly: false
    property string mintError: ""
    property string filterText: ""
    Timer {
        id: searchDebounce
        interval: 80
        repeat: false
        onTriggered: root.filterText = search.text.trim().toLowerCase()
    }

    // Header
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s16
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2
            Text {
                text: "Assets"
                color: FalconTheme.textPrimary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.pageTitle
                font.bold: true
            }
            Text {
                text: "Canonical registry · every tracked market, one identity each"
                color: FalconTheme.textSecondary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.body
            }
        }
        MetricCard { label: "Assets"; value: String(assets.count); Layout.preferredWidth: 110 }
        MetricCard { label: "Favorites"; value: String(ctrl.favoritesCount); Layout.preferredWidth: 120 }
    }

    // Track-mint row (Solana discovery is mint-driven and explicit)
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s12
        TextField {
            id: mintField
            Layout.preferredWidth: 420
            placeholderText: "Track Solana mint… (paste address, Enter to resolve)"
            color: FalconTheme.textPrimary
            placeholderTextColor: FalconTheme.textMuted
            font.family: "Consolas"
            font.pixelSize: FalconTheme.body
            background: Rectangle {
                color: FalconTheme.bgInput
                border.color: FalconTheme.borderDefault
                radius: FalconTheme.r6
            }
            onAccepted: addBtn.doAdd()
        }
        Rectangle {
            id: addBtn
            property bool hovered: addMouse.containsMouse
            property bool pressed: addMouse.pressed
            Layout.preferredWidth: 90
            Layout.preferredHeight: 34
            radius: FalconTheme.r6
            color: addBtn.pressed ? FalconTheme.bgSelected : (addBtn.hovered ? FalconTheme.bgHover : FalconTheme.bgPanelAlt)
            border.color: addBtn.hovered ? FalconTheme.borderActive : FalconTheme.borderDefault
            function doAdd() {
                if (mintField.text.trim() === "") return
                var err = ctrl.addMint(mintField.text.trim())
                if (err === "") {
                    root.mintError = ""
                    mintField.text = ""
                } else {
                    root.mintError = err
                }
            }
            Text {
                anchors.centerIn: parent
                text: "Track"
                color: FalconTheme.textPrimary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.body
                font.bold: true
            }
            MouseArea {
                id: addMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: addBtn.doAdd()
            }
        }
        Text {
            visible: root.mintError !== ""
            text: root.mintError
            color: FalconTheme.negative
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.small
        }
    }

    // Filter bar (all filters operate on registry metadata)
    RowLayout {
        Layout.fillWidth: true
        spacing: FalconTheme.s12
        TextField {
            id: search
            Layout.preferredWidth: 240
            placeholderText: "Search symbol, name, mint…"
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
            model: ["All"].concat(ctrl.assetProviders)
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.body
            background: Rectangle {
                color: FalconTheme.bgInput
                border.color: FalconTheme.borderDefault
                radius: FalconTheme.r6
            }
        }
        ComboBox {
            id: chainBox
            Layout.preferredWidth: 150
            model: ["All"].concat(ctrl.assetChains)
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.body
            background: Rectangle {
                color: FalconTheme.bgInput
                border.color: FalconTheme.borderDefault
                radius: FalconTheme.r6
            }
        }
        ComboBox {
            id: quoteBox
            Layout.preferredWidth: 130
            model: ["All"].concat(ctrl.assetQuotes)
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.body
            background: Rectangle {
                color: FalconTheme.bgInput
                border.color: FalconTheme.borderDefault
                radius: FalconTheme.r6
            }
        }
        Rectangle {
            id: favBtn
            property bool hovered: favMouse.containsMouse
            property bool pressed: favMouse.pressed
            Layout.preferredWidth: 44
            Layout.preferredHeight: 34
            radius: FalconTheme.r6
            color: root.favOnly ? "#2a2118" : (favBtn.pressed ? FalconTheme.bgSelected : (favBtn.hovered ? FalconTheme.bgHover : FalconTheme.bgInput))
            border.color: root.favOnly ? FalconTheme.accentGold : (favBtn.hovered ? FalconTheme.borderActive : FalconTheme.borderDefault)
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
            id: recentBtn
            property bool hovered: recentMouse.containsMouse
            property bool pressed: recentMouse.pressed
            Layout.preferredWidth: 90
            Layout.preferredHeight: 34
            radius: FalconTheme.r6
            color: root.recentOnly ? "#2a2118" : (recentBtn.pressed ? FalconTheme.bgSelected : (recentBtn.hovered ? FalconTheme.bgHover : FalconTheme.bgInput))
            border.color: root.recentOnly ? FalconTheme.accentGold : (recentBtn.hovered ? FalconTheme.borderActive : FalconTheme.borderDefault)
            Text {
                anchors.centerIn: parent
                text: "Recent"
                color: root.recentOnly ? FalconTheme.accentGold : FalconTheme.textMuted
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.small
                font.bold: root.recentOnly
            }
            MouseArea {
                id: recentMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.recentOnly = !root.recentOnly
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
                onClicked: { search.text = ""; root.filterText = ""; providerBox.currentIndex = 0; chainBox.currentIndex = 0; quoteBox.currentIndex = 0; root.favOnly = false; root.recentOnly = false }
            }
        }
    }

    // Registry table
    Panel {
        objectName: "assetsTablePanel"
        title: ""
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredHeight: 480
        ListView {
            id: table
            anchors.fill: parent
            model: assets
            clip: true
            delegate: Rectangle {
                property bool match: (root.filterText === "" || searchHay.indexOf(root.filterText) >= 0)
                    && (providerBox.currentText === "All" || provider === providerBox.currentText)
                    && (chainBox.currentText === "All" || chain === chainBox.currentText
                        || (chainBox.currentText === "CEX" && chain === ""))
                    && (quoteBox.currentText === "All" || quote === quoteBox.currentText)
                    && (!root.favOnly || favorite)
                    && (!root.recentOnly || ctrl.recentKeys.indexOf(key) >= 0)
                visible: match
                height: match ? 52 : 0
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
                        Layout.preferredWidth: 200
                        spacing: 0
                        Text { text: symbol; color: FalconTheme.textPrimary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true }
                        Text { text: name !== "" ? name : (mint !== "" ? mint.slice(0, 8) + "…" : venue); color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.micro }
                    }
                    Text { text: chain !== "" ? chain : "—"; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 80 }
                    Text { text: quote !== "" ? quote : "—"; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 70 }
                    Text { text: priceText; color: direction > 0 ? FalconTheme.positive : (direction < 0 ? FalconTheme.negative : FalconTheme.textPrimary); font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body; font.bold: true; Layout.preferredWidth: 120; horizontalAlignment: Text.AlignRight }
                    Text { text: provider; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 90 }
                    Text { text: feed; color: connected ? (stale ? FalconTheme.warning : FalconTheme.positive) : FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small; Layout.preferredWidth: 130 }
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
                    Text { Layout.preferredWidth: 200; text: "ASSET"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                    Text { Layout.preferredWidth: 80; text: "CHAIN"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                    Text { Layout.preferredWidth: 70; text: "QUOTE"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                    Text { Layout.preferredWidth: 120; text: "PRICE"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily; horizontalAlignment: Text.AlignRight }
                    Text { Layout.preferredWidth: 90; text: "PROVIDER"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                    Text { Layout.preferredWidth: 130; text: "FEED"; color: FalconTheme.textMuted; font.pixelSize: FalconTheme.tableHeader; font.family: FalconTheme.fontFamily }
                }
            }
            footer: Text {
                width: table.width
                text: assets.count + " tracked markets · registry is the single source of truth"
                color: FalconTheme.textMuted
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.small
                leftPadding: FalconTheme.s12
                topPadding: FalconTheme.s8
            }
        }
    }
}
