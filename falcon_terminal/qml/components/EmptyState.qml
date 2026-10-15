import QtQuick
import QtQuick.Layouts

ColumnLayout {
    property string title: ""
    property string body: "Waiting for analysis engine"
    spacing: FalconTheme.s8
    Text {
        text: title
        color: FalconTheme.textMuted
        font.family: FalconTheme.fontFamily
        font.pixelSize: FalconTheme.small
    }
    Text {
        text: body
        color: FalconTheme.textSecondary
        font.family: FalconTheme.fontFamily
        font.pixelSize: FalconTheme.body
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
}
