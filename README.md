# vc-keeper

Keeps your Discord account in a voice channel 24/7. Built on discord.py-self.
Automating a user account is against Discord's ToS: use at your own risk.

## install (debian)
    unzip vc-keeper.zip && cd vc-keeper && sudo bash install.sh

## everyday stuff
    journalctl -u vc-keeper -f            # logs
    sudo nano /etc/vc-keeper.env          # settings
    sudo systemctl restart vc-keeper      # apply settings
    sudo systemctl stop vc-keeper         # leave the vc

## exit codes
0 stopped, 1 crashed (systemd restarts it after 30s), 78 bad config/token