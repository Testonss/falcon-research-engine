import QtQuick

Rectangle {
    property string text: "—"
    property color fg: FalconTheme.textSecondary
    property color bg: FalconTheme.bgPanelAlt
    property color line: FalconTheme.borderDefault
    property int textSize: FalconTheme.micro
    property bool bold: false
    implicitWidth: label.implicitWidth + 16
    implicitHeight: 22
    color: bg
    border.color: line
    border.width: 1
    radius: FalconTheme.r4
    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: fg
        font.family: FalconTheme.fontFamily
        font.pixelSize: textSize
        font.bold: bold
    }
    // alias target
    id: root
}
