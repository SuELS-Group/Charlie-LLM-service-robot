"""Short deterministic safety/error messages; never invoke a model."""
import re

from .language import supported_language

_MESSAGES = {
    'clarify': ('What information should I use to continue?', 'Welche Information soll ich verwenden, um fortzufahren?'),
    'unsupported': ('I cannot carry out that request with my available capabilities.',
                    'Ich kann diesen Auftrag mit meinen verfügbaren Fähigkeiten nicht ausführen.'),
    'clarification_expired': ('The reply window has ended. Please give me the full request again, starting with Charlie.',
                              'Das Antwortfenster ist abgelaufen. Bitte gib mir den vollständigen Auftrag erneut und beginne mit Charlie.'),
    'no_pending_question': ('I am not waiting for a confirmation. Please give me the full request, starting with Charlie.',
                            'Ich warte auf keine Bestätigung. Bitte gib mir den vollständigen Auftrag und beginne mit Charlie.'),
    'battery_green': ('My battery status is green.', 'Mein Akkustatus ist grün.'),
    'battery_yellow': ('My battery status is yellow.', 'Mein Akkustatus ist gelb.'),
    'battery_orange': ('My battery is low. Please arrange a charging break.',
                       'Mein Akku ist schwach. Bitte plane eine Ladepause ein.'),
    'battery_red': ('My battery is critically low. Please recharge me.',
                    'Mein Akku ist kritisch niedrig. Bitte lade mich auf.'),
    'battery_update': ('My battery status has changed.', 'Mein Akkustatus hat sich geändert.'),
    'failed': ('I could not complete this request.', 'Ich konnte diesen Auftrag nicht abschließen.'),
    'invalid_action_arguments': ('I could not prepare this action correctly. I did not start this step.',
                                 'Ich konnte diese Aktion nicht korrekt vorbereiten. Diesen Schritt habe ich nicht gestartet.'),
    'object_state_check': ('My gripper state is unclear. Please tell me explicitly whether it is empty or I am holding an item.',
                           'Mein Greiferzustand ist unklar. Bitte sage mir ausdrücklich, ob mein Greifer leer ist oder ich einen Gegenstand halte.'),
    'already_holding': ('A previous pickup is still recorded. If my gripper is actually empty, please say: Charlie, your gripper is empty.',
                        'Ich habe noch eine frühere Aufnahme gespeichert. Wenn mein Greifer tatsächlich leer ist, sage bitte: Charlie, dein Greifer ist leer.'),
    'pickup_assumed': ('The gripper close finished. I am assuming I hold the item.',
                       'Der Greifvorgang ist abgeschlossen. Ich gehe davon aus, dass ich den Gegenstand halte.'),
    'arm_calibrated': ('Arm calibration is complete.', 'Die Armkalibrierung ist abgeschlossen.'),
    'calibration_state_sync_failed': ('Arm calibration is complete, but I could not confirm the saved gripper state. Please tell me whether my gripper is empty.',
                                      'Die Armkalibrierung ist abgeschlossen, aber ich konnte den gespeicherten Greiferzustand nicht bestätigen. Bitte sage mir, ob mein Greifer leer ist.'),
    'arm_stowed': ('The arm stow operation is complete.', 'Das Einfahren des Arms ist abgeschlossen.'),
    'no_object': ('My gripper is recorded as empty. If I am holding an item, please say: Charlie, you are holding an item.',
                  'Mein Greifer ist als leer vermerkt. Wenn ich einen Gegenstand halte, sage bitte: Charlie, du hältst einen Gegenstand.'),
    'empty_report_recorded': ('I have recorded your correction: my gripper is empty.',
                              'Ich habe deine Korrektur übernommen: Mein Greifer ist leer.'),
    'holding_report_recorded': ('I have recorded your correction: I am holding an item.',
                                'Ich habe deine Korrektur übernommen: Ich halte einen Gegenstand.'),
    'object_report_busy': ('I could not save your correction yet. Please give me a fresh gripper-state report.',
                           'Deine Korrektur habe ich noch nicht gespeichert. Bitte teile mir den Greiferzustand erneut mit.'),
    'object_report_failed': ('I could not save your correction. Please give me a fresh gripper-state report.',
                             'Ich konnte deine Korrektur nicht speichern. Bitte teile mir den Greiferzustand erneut mit.'),
    'object_report_queued': ('I have noted your correction and will record it next.',
                             'Deine Korrektur ist vorgemerkt. Ich übernehme sie als Nächstes.'),
    'object_query_empty': ('My gripper is recorded as empty.', 'Mein Greifer ist als leer vermerkt.'),
    'object_query_holding': ('I have recorded that I am holding an item.', 'Ich habe gespeichert, dass ich einen Gegenstand halte.'),
    'object_query_failed': ('I cannot read my saved gripper state at the moment.',
                           'Ich kann meinen gespeicherten Greiferzustand gerade nicht abrufen.'),
    'empty_report_busy': ('I am still finishing the previous motion. I have not saved your correction. Please tell me again once I have stopped.',
                          'Ich beende noch die vorherige Bewegung. Deine Korrektur habe ich noch nicht gespeichert. Bitte sage sie mir danach erneut.'),
    'empty_report_failed': ('I could not save your correction. Please give me a fresh request or ask the operator to check.',
                            'Ich konnte deine Korrektur nicht speichern. Bitte gib mir einen neuen Auftrag oder lass die Bedienperson nachsehen.'),
    'study_action_disabled': ('This action is not enabled for the current study.',
                              'Diese Aktion ist für die aktuelle Studie nicht freigegeben.'),
    'study_destination_disabled': ('That destination is not approved for this study.',
                                   'Dieses Ziel ist für die Studie nicht freigegeben.'),
    'study_setup_required': ('Please ask the operator to check the study configuration.',
                             'Bitte lass die Bedienperson die Studienkonfiguration prüfen.'),
    'recipient_ambiguous': ('I cannot identify the intended recipient unambiguously at the moment.',
                            'Ich kann die Zielperson gerade nicht eindeutig zuordnen.'),
    'recipient_missing': ('I cannot currently see the requested recipient.',
                          'Ich kann die gewünschte Person gerade nicht sehen.'),
    'recipient_view_unavailable': ('I do not have current camera data for the handover.',
                                   'Ich habe gerade keine aktuellen Kameradaten für die Übergabe.'),
    'recipient_position_invalid': ('I cannot currently determine the recipient position reliably.',
                                   'Ich kann die Position der Zielperson gerade nicht zuverlässig bestimmen.'),
    'recipient_out_of_range': ('The selected person is outside my handover range.',
                               'Die ausgewählte Person steht außerhalb meiner Übergabereichweite.'),
    'recipient_changed': ('I have lost reliable tracking of the selected person. Please give me a fresh handover request.',
                          'Ich habe die ausgewählte Person nicht mehr zuverlässig im Blick. Bitte gib mir einen neuen Übergabeauftrag.'),
    'known_travel_posture': ('Before driving, I also need to retract my arm.',
                             'Vor einer Fahrt muss ich außerdem meinen Arm einfahren.'),
    'arm_not_stowed': ('I cannot drive because my arm is not in its travel position. Please have the arm checked and stowed before trying again.',
                      'Ich kann nicht fahren, weil mein Arm nicht in Fahrposition ist. Bitte lass den Arm prüfen und verstauen, bevor du es erneut versuchst.'),
    'arm_contact_before_release': ('Contact detection stopped the arm movement. I have not opened the gripper. Please check that the arm is clear.',
                                 'Die Kontakterkennung hat die Armbewegung gestoppt. Ich habe den Greifer nicht geöffnet. Bitte prüfe, ob der Arm frei ist.'),
    'trash_target_missing': ('I could not locate the nearby trash can. I have not opened the gripper. Please check my position and the bin marker.',
                             'Ich konnte den nahen Mülleimer nicht lokalisieren. Ich habe den Greifer nicht geöffnet. Bitte prüfe meine Position und die Markierung am Mülleimer.'),
    'trash_target_unreachable': ('I located the trash can, but cannot reach it from this position. I have not opened the gripper.',
                                 'Ich habe den Mülleimer lokalisiert, kann ihn aus dieser Position aber nicht erreichen. Ich habe den Greifer nicht geöffnet.'),
    'person_camera_unavailable': ('I cannot check the person with my camera at the moment. Please ask the operator to check.',
                                  'Ich kann die Person gerade nicht mit meiner Kamera prüfen. Bitte lass die Bedienperson nachsehen.'),
    'travel_unconfirmed': ('I could not confirm that I am ready to move. Please ask the operator to check.',
                           'Ich konnte nicht bestätigen, dass ich wieder fahren kann. Bitte lass die Bedienperson meinen Zustand prüfen.'),
    'opened_travel_unconfirmed': ('My gripper opened, but I could not confirm that I am ready to move. Please ask the operator to check.',
                                  'Mein Greifer wurde geöffnet, aber ich konnte nicht bestätigen, dass ich wieder fahren kann. Bitte lass die Bedienperson meinen Zustand prüfen.'),
    'uncertain': ("I couldn't confirm the result. I won't repeat this request automatically.",
                  'Ich konnte das Ergebnis nicht bestätigen. Ich wiederhole diesen Auftrag nicht automatisch.'),
    'timeout': ('This request took too long. I could not confirm the result.',
                'Dieser Auftrag hat zu lange gedauert. Ich konnte das Ergebnis nicht bestätigen.'),
    'planning_timeout': ('I could not plan this request in time. Please give me a new request.',
                         'Ich konnte diesen Auftrag nicht rechtzeitig planen. Bitte gib einen neuen Auftrag.'),
    'planning_output_limit': ('My next-step planning was too long even after one retry. I have not started any further actions.',
                              'Die Planung des nächsten Schritts war auch beim zweiten Versuch zu lang. Ich habe keine weiteren Aktionen gestartet.'),
    'planning_context_capacity': ('There is not enough context space to continue planning. Please split the request into smaller parts.',
                                  'Für die weitere Planung reicht das Kontextfenster nicht aus. Bitte teile den Auftrag in kleinere Teile auf.'),
    'planning_invalid': ('I could not produce a valid next step. No further action was started.',
                         'Ich konnte keinen gültigen nächsten Schritt planen. Es wurde keine weitere Aktion gestartet.'),
    'planning_loop': ('I stopped because planning repeatedly selected the same action. No further action was started.',
                      'Ich habe abgebrochen, weil die Planung immer wieder dieselbe Aktion gewählt hat. Es wurde keine weitere Aktion gestartet.'),
    'planning_decision_limit': ('I reached the processing limit and ended this request. Please give the unfinished part as a new request.',
                                'Ich habe die Bearbeitungsgrenze erreicht und diesen Auftrag beendet. Bitte gib den offenen Teil als neuen Auftrag.'),
    'planning_service_error': ('A technical error interrupted planning. No further action was started.',
                               'Ein technischer Fehler hat die Planung unterbrochen. Es wurde keine weitere Aktion gestartet.'),
    'planning_timeout_after_success': ('The last step finished, but I could not continue in time. Please give me a new request.',
                                       'Der letzte Schritt ist abgeschlossen, aber die weitere Bearbeitung hat zu lange gedauert. Bitte gib einen neuen Auftrag.'),
    'planning_failure_after_success': ('The last step finished, but I could not complete the rest of your request.',
                                       'Der letzte Schritt ist abgeschlossen, aber ich konnte den restlichen Auftrag nicht abschließen.'),
    'cancelled': ('The task is cancelled.', 'Der Auftrag ist abgebrochen.'),
    'request_replaced': ('I am dropping the previous request and will handle your new request.',
                         'Ich verwerfe den bisherigen Auftrag und kümmere mich um deine neue Anfrage.'),
    'recovery_required': ('Check that I am stationary and whether my gripper is empty or holding an item. '
                          'Tell me "Charlie, your gripper is empty" or "Charlie, you are holding an item". '
                          'You can also ask me to calibrate and then stow my arm; I check controller readiness first.',
                          'Prüfe, ob ich stillstehe und ob mein Greifer leer ist oder einen Gegenstand hält. '
                          'Sage „Charlie, dein Greifer ist leer“ oder „Charlie, du hältst einen Gegenstand“. '
                          'Du kannst mich auch bitten, meinen Arm zu kalibrieren und danach zu verstauen; '
                          'ich prüfe vorher die Steuerung.'),
    'recovery_blocked': ('The previous action still needs a state check. I did not start this step.',
                         'Der Zustand nach der vorherigen Aktion muss noch geprüft werden. Diesen Schritt habe ich nicht gestartet.'),
    'recovery_finishing': ('I am still finishing the previous action. I did not start this step. Please wait briefly and give me a new request.',
                           'Ich beende noch die vorherige Aktion. Diesen Schritt habe ich nicht gestartet. Bitte warte kurz und gib mir dann einen neuen Auftrag.'),
    'recovery_recheck': ('With your next request, I will first check whether I can continue.',
                        'Bei deinem nächsten Auftrag prüfe ich zuerst, ob ich fortfahren kann.'),
    'busy': ('I am still finishing the previous action. Please wait.',
             'Ich beende noch die vorherige Aktion. Bitte warte.'),
    'request_busy': ('I am still processing the current request. Please wait briefly or say stop.',
                     'Ich bearbeite noch den aktuellen Auftrag. Bitte warte kurz oder sage Stopp.'),
    'input_expired': ('That request arrived too late. Please give me a fresh request.',
                      'Diese Anfrage kam zu spät an. Bitte gib mir einen neuen Auftrag.'),
    'completed_late': ('The action finished after its time limit. I will not repeat it.',
                       'Die Aktion wurde nach ihrem Zeitlimit abgeschlossen. Ich wiederhole sie nicht.'),
    'timeout_cancelled': ('The action timed out and its cancellation was confirmed.',
                          'Die Aktion hat zu lange gedauert. Ihr Abbruch wurde bestätigt.'),
    'arm_unconfirmed': ('I could not confirm that my arm retracted.',
                        'Ich konnte nicht bestätigen, dass mein Arm eingefahren ist.'),
    'grab_cleanup_unconfirmed': ('The grasp movement finished, but stowing or restoring the controller mode was not confirmed. '
                                'Please give me a new request to stow my arm.',
                                'Die Greifbewegung ist abgeschlossen, aber das Verstauen oder der Moduswechsel konnte nicht bestätigt werden. '
                                'Bitte gib mir einen neuen Auftrag zum Verstauen des Arms.'),
    'opened_arm_unconfirmed': ('My gripper opened, but I could not confirm that my arm retracted.',
                               'Mein Greifer wurde geöffnet, aber ich konnte nicht bestätigen, dass mein Arm eingefahren ist.'),
    'base_reached_incomplete': ('I reached the destination, but could not complete the remaining action.',
                                'Ich habe das Ziel erreicht, konnte aber die verbleibende Aktion nicht abschließen.'),
    'runstop_active': ('The robot safety stop is active. Please check the robot and release the stop when it is safe. Then give me a new request.',
                       'Der Sicherheitsstopp am Roboter ist aktiv. Bitte prüfe den Roboter und löse den Stopp, wenn es sicher ist. Gib mir danach den Auftrag erneut.'),
    'robot_not_homed': ('The arm is not calibrated yet. Please ask me to calibrate and then stow my arm.',
                         'Der Arm ist noch nicht kalibriert. Bitte beauftrage mich, meinen Arm zu kalibrieren und danach einzufahren.'),
    'arm_driver_busy': ('Another movement or calibration is still running. Please wait until it finishes and give me a new request.',
                         'Eine andere Bewegung oder Kalibrierung läuft noch. Bitte warte, bis sie fertig ist, und gib mir einen neuen Auftrag.'),
    'arm_service_unavailable': ('The arm controller is currently unavailable. I did not start this step. Please ask the operator to check the connection.',
                                'Die Armsteuerung ist gerade nicht erreichbar. Diesen Schritt habe ich nicht gestartet. Bitte lass die Bedienperson die Verbindung prüfen.'),
    'driver_state_unavailable': ('I do not have a fresh status from the robot controller. I did not start this step. Please wait briefly and give me the request again.',
                                 'Mir fehlt eine aktuelle Statusmeldung der Robotersteuerung. Diesen Schritt habe ich nicht gestartet. Bitte warte kurz und gib mir den Auftrag erneut.'),
    'arm_driver_mode': ('The arm controller cannot accept this action in its current mode. I did not start this step. Please ask the operator to check the controller.',
                         'Die Armsteuerung kann diese Aktion im aktuellen Modus nicht annehmen. Diesen Schritt habe ich nicht gestartet. Bitte lass die Bedienperson die Steuerung prüfen.'),
    'arm_calibration_failed': ('The arm controller reported that calibration failed. Please ask the operator to check the arm before a new request.',
                               'Die Armsteuerung hat einen Fehler bei der Kalibrierung gemeldet. Bitte lass die Bedienperson den Arm vor einem neuen Auftrag prüfen.'),
    'arm_stow_failed': ('The arm controller reported that stowing failed. Please ask the operator to check the arm before a new request.',
                        'Die Armsteuerung hat einen Fehler beim Einfahren gemeldet. Bitte lass die Bedienperson den Arm vor einem neuen Auftrag prüfen.'),
    'head_preparation_failed': ('I could not position my head for navigation, so I did not start driving.',
                                'Ich konnte meinen Kopf nicht für die Navigation positionieren. Deshalb habe ich die Fahrt nicht gestartet.'),
    'gesture_timeout': ('I did not receive your confirmation in time. Please ask again when you are ready.',
                        'Ich habe deine Bestätigung nicht rechtzeitig erhalten. Bitte gib einen neuen Auftrag, wenn du bereit bist.'),
}


def runtime_message(key: str, language: str = 'en') -> str:
    return _MESSAGES[key][1 if supported_language(language) == 'de' else 0]


def battery_notification_level(text: str) -> str:
    match = re.match(r'^Battery status (green|yellow|orange|red)\b', text, flags=re.IGNORECASE)
    return match.group(1).lower() if match else ('red' if text.strip() == 'Battery critical.' else 'update')


def battery_notification(text: str, language: str = 'en') -> str:
    """Announce an event without executing its configured task instructions."""
    level = battery_notification_level(text)
    return runtime_message('battery_' + level, language)


_ACTION_STARTS = {
    'move_to': ('I will move to the requested position.', 'Ich fahre zur gewünschten Position.'),
    'move_to_waypoint': ('I will drive to the requested service point.', 'Ich fahre zum gewünschten Servicepunkt.'),
    'move_relative': ('I will move my base.', 'Ich bewege meine Basis.'),
    'drive_to_person': ('I will look for the person and approach them.', 'Ich suche die Person und fahre zu ihr.'),
    'turn_to': ('I will turn.', 'Ich drehe mich.'),
    'turn_to_relative': ('I will turn.', 'Ich drehe mich.'),
    'position_for_action': ('I will position myself for the action.', 'Ich positioniere mich für die Aktion.'),
    'take_from_hand': ('I will approach for the pickup. Please wait for my instructions.',
                       'Ich nähere mich für die Übergabe. Bitte warte auf meine Anweisungen.'),
    'give_to_person': ('I will approach for the handover. Please wait for my instructions.',
                       'Ich nähere mich für die Übergabe. Bitte warte auf meine Anweisungen.'),
    'grab_item': ('I will try to grasp the item.', 'Ich versuche, den Gegenstand zu greifen.'),
    'place_item': ('I will try to place the item.', 'Ich versuche, den Gegenstand abzulegen.'),
    'put_item_in_trash': ('I will put the held item into the trash can.', 'Ich lege den gehaltenen Gegenstand in den Mülleimer.'),
    'release_item': ('I will open my gripper and retract my arm.', 'Ich öffne meinen Greifer und fahre meinen Arm ein.'),
    'dock_robot': ('I will drive to the docking station and check charging.',
                   'Ich fahre zur Ladestation und prüfe den Ladevorgang.'),
    'start_following': ('I will start following.', 'Ich beginne zu folgen.'),
    'stop_following': ('I will stop following.', 'Ich beende das Folgen.'),
    'get_look_around_objects': ('I will look around.', 'Ich schaue mich um.'),
    'wave_hand': ('I will wave my arm.', 'Ich winke mit meinem Arm.'),
    'move_head': ('I will move my head.', 'Ich bewege meinen Kopf.'),
}


def action_start_message(tool: str, language: str = 'en') -> str:
    messages = _ACTION_STARTS.get(tool)
    return messages[1 if supported_language(language) == 'de' else 0] if messages else ''


def result_failure_message(result, language='en'):
    error = result.error_details or {}
    data = result.result.get('result') if isinstance(result.result, dict) else {}
    data = data if isinstance(data, dict) else {}
    code = error.get('code')
    if data.get('calibration_completed') is True and data.get('calibration_state_reset') is False:
        return runtime_message('calibration_state_sync_failed', language)
    if code == 'RECOVERY_REQUIRED' and error.get('execution_started') is False and not error.get('execution_uncertain'):
        if error.get('recovery_check_pending'):
            return runtime_message('recovery_finishing', language)
        return runtime_message('recovery_blocked', language) + ' ' + runtime_message('recovery_required', language)
    if code in {'POLICY_DENIED', 'OBJECT_NOT_APPROVED'}:
        return runtime_message('study_action_disabled', language)
    if code == 'WAYPOINT_NOT_APPROVED':
        return runtime_message('study_destination_disabled', language)
    if code in {'STUDY_PROFILE_NOT_READY', 'STUDY_PROFILE_UNAVAILABLE'}:
        return runtime_message('study_setup_required', language)
    recipient_messages = {'RECIPIENT_AMBIGUOUS':'recipient_ambiguous',
        'TARGET_PERSON_NOT_FOUND':'recipient_missing', 'NO_PERSON_FOUND':'recipient_missing',
        'RECIPIENT_NOT_CONFIRMED':'recipient_ambiguous',
        'RECIPIENT_VIEW_UNAVAILABLE':'recipient_view_unavailable',
        'RECIPIENT_POSITION_INVALID':'recipient_position_invalid',
        'RECIPIENT_TRACKING_UNAVAILABLE':'recipient_changed', 'RECIPIENT_CHANGED':'recipient_changed',
        'RECIPIENT_OUT_OF_RANGE':'recipient_out_of_range'}
    recipient_code = data.get('error_code', code)
    if recipient_code in recipient_messages:
        message = runtime_message(recipient_messages[recipient_code], language)
        if error.get('execution_uncertain') and data.get('arm_motion_started') and not data.get('arm_retracted'):
            message += ' ' + runtime_message('arm_unconfirmed', language)
        elif data.get('posture_error') and not error.get('execution_uncertain'):
            message += ' ' + runtime_message('known_travel_posture', language)
        return message
    if code == 'PERSON_VIEW_UNAVAILABLE':
        return runtime_message('person_camera_unavailable', language)
    if error.get('code') in {'OBJECT_STATE_INVALID', 'OBJECT_STATE_UNAVAILABLE'}:
        state = data.get('possession')
        return runtime_message('already_holding' if state == 'holding_assumed' else
                               'no_object' if state == 'empty' else 'object_state_check', language)
    if result.tool_name == 'grab_item' and data.get('grasp_completed') is True and data.get('cleanup_error'):
        return runtime_message('grab_cleanup_unconfirmed', language)
    controller_error = data.get('controller_error')
    if (isinstance(controller_error, dict) and controller_error.get('code') == 'CONTACT_DETECTED'
            and not error.get('execution_uncertain') and result.status.value != 'pending'
            and data.get('release_attempted') is False and data.get('gripper_opened') is False):
        message = runtime_message('arm_contact_before_release', language)
        if data.get('travel_ready') is False:
            message += ' ' + runtime_message('known_travel_posture', language)
        return message
    if data.get('travel_ready') is False:
        if data.get('arm_retracted') is True:
            return runtime_message('opened_travel_unconfirmed' if data.get('gripper_opened') else 'travel_unconfirmed', language)
        return runtime_message('opened_arm_unconfirmed' if data.get('gripper_opened') else 'arm_unconfirmed', language)
    if data.get('base_reached') is True:
        return runtime_message('base_reached_incomplete', language)
    if error.get('execution_uncertain') or result.status.value == 'pending':
        return runtime_message('uncertain', language)
    if code == 'INVALID_ARGUMENTS' and (error.get('execution_started') is False or data.get('execution_started') is False):
        return runtime_message('invalid_action_arguments', language)
    if error.get('code') == 'RUNSTOP_ACTIVE':
        return runtime_message('runstop_active', language)
    if code == 'ARM_NOT_STOWED' and data.get('base_started') is False:
        return runtime_message('arm_not_stowed', language)
    if code in {'TRASH_TARGET_NOT_FOUND', 'TRASH_TARGET_UNREACHABLE'} and data.get('release_attempted') is False:
        return runtime_message('trash_target_missing' if code == 'TRASH_TARGET_NOT_FOUND' else 'trash_target_unreachable', language)
    arm_failure_messages = {'ROBOT_NOT_HOMED': 'robot_not_homed', 'DRIVER_BUSY': 'arm_driver_busy',
        'ARM_SERVICE_UNAVAILABLE': 'arm_service_unavailable', 'DRIVER_MODE_UNAVAILABLE': 'arm_driver_mode',
        'DRIVER_STATE_UNAVAILABLE': 'driver_state_unavailable',
        'CALIBRATION_FAILED': 'arm_calibration_failed', 'STOW_FAILED': 'arm_stow_failed'}
    if code in arm_failure_messages:
        return runtime_message(arm_failure_messages[code], language)
    if error.get('code') == 'HEAD_PREPARATION_FAILED' and data.get('base_started') is False:
        return runtime_message('head_preparation_failed', language)
    if result.status.value == 'timeout':
        if error.get('code') in {'HAND_PICKUP_TIMEOUT', 'GIVE_TO_PERSON_TIMEOUT'} and data.get('travel_ready'):
            return runtime_message('gesture_timeout', language)
        if data.get('completed_late'):
            return runtime_message('completed_late', language)
        if error.get('cancellation_confirmed'):
            return runtime_message('timeout_cancelled', language)
        return runtime_message('timeout', language)
    return runtime_message('failed', language)
