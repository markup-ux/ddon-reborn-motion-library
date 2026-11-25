local motion_id = 1
local probes = {
    {label = "Sword candidate", bank = 21440, last = 30,
        resource = "animation\\ddon_reborn\\fighter_sword_probe.motlist"},
    {label = "Short control", bank = 21439, last = 15,
        resource = "animation\\ddon_reborn\\fighter_probe.motlist"},
}
-- Probe runner
local status = "Load a save and stand idle before starting."
local queued = nil
local playing = nil
local change_motion = "changeMotion(System.UInt32, System.UInt32, System.Single, System.Single, via.motion.InterpolationMode, via.motion.InterpolationCurve)"

local function stop_playing()
    local test = playing
    playing = nil
    if test and test.probe.on_stop then
        test.probe.on_stop(test)
    end
end

local function finish(message, failed)
    stop_playing()
    status = message
    if failed then
        log.error("[DDON Reborn probe] " .. message)
    else
        log.info("[DDON Reborn probe] " .. message)
    end
end

local function find_bank(component, bank_id)
    for index = 0, component:getDynamicMotionBankCount() - 1 do
        local bank = component:getDynamicMotionBank(index)
        if bank and bank:get_BankID() == bank_id then
            return bank
        end
    end
end

local function is_probe_bank(bank, resource)
    local motion_list = bank and bank:get_MotionList()
    if not motion_list then
        return false
    end
    local name = motion_list:ToString():lower():gsub("/", "\\")
    return name:find(resource, 1, true) ~= nil
end

local function play(probe)
    if probe.prepare then
        probe.prepare()
    end
    local manager = sdk.get_managed_singleton("app.CharacterManager")
    local player = manager and manager:get_ManualPlayer()
    assert(player and player:get_Valid(), "No active Arisen. Load a save first.")
    local component = player:get_Motion()
    local layer = component and component:getLayer(0)
    assert(layer, "The active Arisen has no motion layer 0.")

    local motion = require("_NickCore/Utils/Motion")
    local bank = find_bank(component, probe.bank)
    if bank then
        assert(is_probe_bank(bank, probe.resource), "Another mod uses DDON Reborn's probe bank ID.")
    else
        motion.add_dynamic_motionbank(component, probe.resource, probe.bank)
        assert(is_probe_bank(find_bank(component, probe.bank), probe.resource),
            "The probe MOTLIST did not load. Check the DD2 resource loader log.")
    end

    playing = {probe = probe, layer = layer, ticks = 0, shown = 0, frame = 0}
    if probe.on_start then
        probe.on_start(playing, player)
    end
    status = "Waiting for the DD2 motion update."
    log.info(("[DDON Reborn probe] %s bound to dynamic bank %d."):format(probe.label, probe.bank))
end

re.on_frame(function()
    if queued and os.time() >= queued.deadline then
        local probe = queued.probe
        queued = nil
        local ok, err = pcall(play, probe)
        if not ok then
            finish(tostring(err), true)
        end
    end
end)

re.on_script_reset(stop_playing)

re.on_pre_application_entry("UpdateMotion", function()
    local test = playing
    if not test then
        return
    end
    local ok, err = pcall(function()
        test.ticks = test.ticks + 1
        local layer = test.layer
        local bank = test.probe.bank
        if test.ticks == 1 or layer:get_MotionBankID() ~= bank or layer:get_MotionID() ~= motion_id then
            layer:call(change_motion, bank, motion_id, test.frame, 0.0, 2, 0)
        end
    end)
    if not ok then
        finish(tostring(err), true)
    end
end)

re.on_application_entry("UpdateMotion", function()
    local test = playing
    if not test or test.ticks == 0 then
        return
    end
    local ok, err = pcall(function()
        local layer = test.layer
        local node = layer:get_HighestWeightMotionNode()
        local bank = node and node:get_MotionBankID()
        local id = node and node:get_MotionID()
        if bank == test.probe.bank and id == motion_id then
            test.shown = test.shown + 1
            test.frame = math.max(test.frame, layer:get_Frame())
            if test.probe.frame_observed then
                test.probe.frame_observed(test)
            end
            if test.shown == 1 then
                log.info("[DDON Reborn probe] DD2 evaluated the probe motion.")
            end
        end
        if test.frame >= test.probe.last then
            finish(("%s evaluated through frame %.1f."):format(test.probe.label, test.frame), false)
        elseif test.ticks >= math.max(240, test.probe.last * 8) or (test.ticks >= 8 and test.shown == 0) then
            finish(("%s evaluated on %d updates. Last active bank %s, motion %s."):format(
                test.probe.label, test.shown, tostring(bank), tostring(id)), test.shown == 0)
        end
    end)
    if not ok then
        finish(tostring(err), true)
    end
end)

re.on_draw_ui(function()
    if imgui.collapsing_header("DDON Reborn - Fighter playback probe") then
        imgui.text(status)
        if not playing and not queued then
            for _, probe in ipairs(probes) do
                if probe.draw_settings then probe.draw_settings() end
            end
            for _, probe in ipairs(probes) do
                if imgui.button(probe.label .. " in 3 seconds") then
                    queued = {probe = probe, deadline = os.time() + 3}
                    status = "Close the overlay and watch the Arisen."
                    break
                end
            end
        end
        if queued then
            if imgui.button("Cancel") then
                queued = nil
                status = "Playback cancelled."
            end
        end
    end
end)
