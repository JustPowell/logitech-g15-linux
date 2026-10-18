# fish completions for g15ctl. Install: cp packaging/g15ctl.fish ~/.config/fish/completions/

function __g15ctl_pages
    # Live page list from the daemon; fall back to the built-in names.
    set -l out
    if command -sq g15ctl
        set out (g15ctl pages 2>/dev/null | string match -rg '"pages": \[([^]]*)\]' | string match -arg '"([a-z0-9_]+)"')
    end
    if test (count $out) -gt 0
        printf '%s\n' $out
    else
        printf '%s\n' clock sysstats media custom
    end
end

set -l cmds notify text image page pages backlight ping

complete -c g15ctl -f
complete -c g15ctl -l socket -r -F -d 'Socket path'
complete -c g15ctl -s h -l help -d 'Show help'

complete -c g15ctl -n "not __fish_seen_subcommand_from $cmds" -a notify -d 'Show a notification overlay'
complete -c g15ctl -n "not __fish_seen_subcommand_from $cmds" -a text -d 'Set custom page text'
complete -c g15ctl -n "not __fish_seen_subcommand_from $cmds" -a image -d 'Show an image on the custom page'
complete -c g15ctl -n "not __fish_seen_subcommand_from $cmds" -a page -d 'Switch page'
complete -c g15ctl -n "not __fish_seen_subcommand_from $cmds" -a pages -d 'List pages'
complete -c g15ctl -n "not __fish_seen_subcommand_from $cmds" -a backlight -d 'Set backlight level'
complete -c g15ctl -n "not __fish_seen_subcommand_from $cmds" -a ping -d 'Check the daemon is alive'

complete -c g15ctl -n '__fish_seen_subcommand_from notify' -s t -l title -r -d 'Title line'
complete -c g15ctl -n '__fish_seen_subcommand_from notify' -s s -l seconds -r -d 'Seconds to show'
complete -c g15ctl -n '__fish_seen_subcommand_from text' -l align -x -a 'left center' -d 'Text alignment'
complete -c g15ctl -n '__fish_seen_subcommand_from text image' -l show -d 'Switch to the custom page'
complete -c g15ctl -n '__fish_seen_subcommand_from image' -F
complete -c g15ctl -n '__fish_seen_subcommand_from page' -a 'next prev (__g15ctl_pages)'
complete -c g15ctl -n '__fish_seen_subcommand_from backlight' -a '0 1 2 toggle'
