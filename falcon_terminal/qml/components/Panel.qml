import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    property string title: ""
    property string linkText: ""
    signal linkClicked()
    default property alias content: contentItem.children
    color: FalconTheme.bgPanel
    border.color: FalconTheme.borderSubtle
    border.width: 1
    radius: FalconTheme.r8

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: FalconTheme.s16
        spacing: FalconTheme.s12
        RowLayout {
            visible: root.title !== ""
            Layout.fillWidth: true
            Text {
                text: root.title
                color: FalconTheme.textPrimary
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.panelTitle
                font.bold: true
                Layout.fillWidth: true
            }
            Text {
                visible: root.linkText !== ""
                text: root.linkText
                color: FalconTheme.accentGold
                font.family: FalconTheme.fontFamily
                font.pixelSize: FalconTheme.small
                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.linkClicked() }
            }
        }
        Item {
            id: contentItem
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }
}
