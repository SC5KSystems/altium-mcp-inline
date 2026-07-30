// Build a schematic circuit from the spec written by dev/make_circuit.py.
//
// Places database-linked components, wires them, and adds junctions, net
// labels, power ports and notes. Afterwards it writes pin_map.txt - every
// placed pin's ABSOLUTE location - so wire routes can be computed from real
// coordinates instead of predicted ones (rotated symbols make prediction
// unreliable).
//
// Spec records (see make_circuit.py):
//   PART|desig|symlib|symbol|designitemid|x|y|orient
//   COMMENT|t   DESCRIPTION|t   FOOTPRINT|n   PARAM|name|value
//   WIRE|x1|y1|x2|y2[|...]   JUNCTION|x|y
//   NETLABEL|x|y|orient|text   POWER|x|y|orient|style|text   NOTE|x|y|text
//
// SAFETY: opening a symbol library makes THAT document current, and stray
// components were once registered into shared libraries on the read-only
// share that way. So the target sheet is created up front, held in TargetDoc,
// and verified to be a schematic (ObjectID 32, not 33 = library) exactly once.
// Everything registers into TargetDoc by reference, never into "current".

SandboxLog('loading circuit spec');
List1 := TStringList.Create;
List1.LoadFromFile('C:\Users\Public\altium_mcp\circuit_spec.txt');
SandboxLog('spec lines: ' + IntToStr(List1.Count));

SandboxLog('creating target schematic');
GetWorkSpace.DM_CreateNewDocument('SCH');
TargetDoc := SchServer.GetCurrentSchDocument;
SandboxLog('target: ' + TargetDoc.DocumentName + ' objectID=' + IntToStr(TargetDoc.ObjectID));

if (TargetDoc.ObjectID <> 32) then
begin
    ResultText := '{"error": "target is not a schematic - refusing to build"}';
    SandboxLog('ABORT: target is not a schematic');
end
else
begin
    SchServer.ProcessControl.PreProcess(TargetDoc, '');

    Obj3 := nil;    // component currently being configured
    S4 := '';       // symbol library currently open (avoids reopening)
    I4 := 0;        // parts placed
    I5 := 0;        // graphics placed (wires/junctions/labels/ports/notes)

    for I3 := 0 to List1.Count - 1 do
    begin
        S1 := List1[I3];
        S2 := SbxField(S1, 0);

        // ---------------------------------------------------------- PART
        if (S2 = 'PART') then
        begin
            S3 := SbxField(S1, 2);                 // symbol library path
            if (S3 <> S4) then
            begin
                SandboxLog('opening library ' + S3);
                Obj2 := Client.OpenDocument('SchLib', S3);
                Client.ShowDocument(Obj2);
                Sleep(1200);
                S4 := S3;
            end;
            Obj1 := SchServer.GetCurrentSchDocument;

            // locate the symbol by LibReference
            Obj3 := nil;
            Obj2 := Obj1.SchLibIterator_Create;
            Obj2.AddFilter_ObjectSet(MkSet(eSchComponent));
            Obj6 := Obj2.FirstSchObject;
            while (Obj6 <> nil) do
            begin
                if (Obj6.LibReference = SbxField(S1, 3)) then Obj3 := Obj6;
                Obj6 := Obj2.NextSchObject;
            end;
            Obj1.SchIterator_Destroy(Obj2);

            if (Obj3 = nil) then
                SandboxLog('  MISSING symbol ' + SbxField(S1, 3) + ' for ' + SbxField(S1, 1))
            else
            begin
                Obj3 := Obj3.Replicate;
                Obj3.Designator.Text := SbxField(S1, 1);
                Obj3.DesignItemID    := SbxField(S1, 4);
                Obj3.Orientation     := StrToInt(SbxField(S1, 7));

                TargetDoc.RegisterSchObjectInContainer(Obj3);
                SchServer.RobotManager.SendMessage(TargetDoc.I_ObjectAddress, c_BroadCast,
                    SCHM_PrimitiveRegistration, Obj3.I_ObjectAddress);

                // Field 8 = "keep text upright". Rotating a part 180 to face
                // its pins the other way also turns its designator, comment
                // and visible parameters upside down. ISch_Component.IsMirrored
                // is not an alternative - it sets True but leaves the pin
                // coordinates unchanged. So undo the rotation on the text only.
                if (SbxField(S1, 8) = '1') then
                begin
                    Obj3.Designator.Orientation := 0;
                    Obj3.Comment.Orientation := 0;
                    Obj2 := Obj3.SchIterator_Create;
                    Obj2.AddFilter_ObjectSet(MkSet(eParameter));
                    Obj6 := Obj2.FirstSchObject;
                    while (Obj6 <> nil) do
                    begin
                        Obj6.Orientation := 0;
                        Obj6 := Obj2.NextSchObject;
                    end;
                    Obj3.SchIterator_Destroy(Obj2);
                    SandboxLog('  text kept upright on ' + SbxField(S1, 1));
                end;

                // MoveByXY (not Location) so the designator/comment text moves too
                Obj3.MoveByXY(
                    MilsToCoord(StrToInt(SbxField(S1, 5)) - CoordToMils(Obj3.Location.X)),
                    MilsToCoord(StrToInt(SbxField(S1, 6)) - CoordToMils(Obj3.Location.Y)));

                I4 := I4 + 1;
                SandboxLog('  placed ' + SbxField(S1, 1) + ' (' + SbxField(S1, 3) + ') at ' +
                           SbxField(S1, 5) + ',' + SbxField(S1, 6) + ' o' + SbxField(S1, 7));
            end;
        end

        // ------------------------------------------ per-component records
        else if (S2 = 'COMMENT') then
        begin
            if (Obj3 <> nil) then Obj3.Comment.Text := SbxField(S1, 1);
        end
        else if (S2 = 'DESCRIPTION') then
        begin
            if (Obj3 <> nil) then Obj3.ComponentDescription := SbxField(S1, 1);
        end
        else if (S2 = 'FOOTPRINT') then
        begin
            if (Obj3 <> nil) then
            begin
                Obj5 := Obj3.AddSchImplementation;
                Obj5.ModelName := SbxField(S1, 1);
                Obj5.ModelType := 'PCBLIB';
                Obj5.IsCurrent := True;
                Obj5.UseComponentLibrary := True;
            end;
        end
        else if (S2 = 'PARAM') then
        begin
            if (Obj3 <> nil) then
            begin
                S5 := SbxField(S1, 1);            // parameter name
                S3 := SbxField(S1, 2);            // parameter value

                // overwrite the symbol's placeholder if it already has one
                B1 := 0;
                Obj2 := Obj3.SchIterator_Create;
                Obj2.AddFilter_ObjectSet(MkSet(eParameter));
                Obj6 := Obj2.FirstSchObject;
                while (Obj6 <> nil) do
                begin
                    if (UpperCase(Obj6.Name) = UpperCase(S5)) then
                    begin
                        Obj6.Text := S3;
                        B1 := 1;
                    end;
                    Obj6 := Obj2.NextSchObject;
                end;
                Obj3.SchIterator_Destroy(Obj2);

                if (B1 = 0) then
                begin
                    Obj6 := SchServer.SchObjectFactory(eParameter, eCreate_Default);
                    Obj6.Name := S5;
                    Obj6.Text := S3;
                    Obj6.ParamType := eParameterType_String;
                    Obj6.ReadOnlyState := eReadOnly_None;
                    Obj6.IsHidden := True;
                    Obj3.AddSchObject(Obj6);
                    SchServer.RobotManager.SendMessage(Obj3.I_ObjectAddress, c_BroadCast,
                        SCHM_PrimitiveRegistration, Obj6.I_ObjectAddress);
                end;
            end;
        end

        // ---------------------------------------------------------- WIRE
        else if (S2 = 'WIRE') then
        begin
            Obj5 := SchServer.SchObjectFactory(eWire, eCreate_GlobalCopy);
            Obj5.Location := Point(MilsToCoord(StrToInt(SbxField(S1, 1))),
                                   MilsToCoord(StrToInt(SbxField(S1, 2))));
            I1 := 1;    // field cursor
            I2 := 0;    // vertex number (1-based)
            while (SbxField(S1, I1) <> '') do
            begin
                I2 := I2 + 1;
                Obj5.InsertVertex := I2;
                Obj5.SetState_Vertex(I2,
                    Point(MilsToCoord(StrToInt(SbxField(S1, I1))),
                          MilsToCoord(StrToInt(SbxField(S1, I1 + 1)))));
                I1 := I1 + 2;
            end;
            TargetDoc.RegisterSchObjectInContainer(Obj5);
            SchServer.RobotManager.SendMessage(TargetDoc.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);
            I5 := I5 + 1;
        end

        // ------------------------------------------------------ JUNCTION
        else if (S2 = 'JUNCTION') then
        begin
            Obj5 := SchServer.SchObjectFactory(eJunction, eCreate_GlobalCopy);
            Obj5.Location := Point(MilsToCoord(StrToInt(SbxField(S1, 1))),
                                   MilsToCoord(StrToInt(SbxField(S1, 2))));
            TargetDoc.RegisterSchObjectInContainer(Obj5);
            SchServer.RobotManager.SendMessage(TargetDoc.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);
            I5 := I5 + 1;
        end

        // ------------------------------------------------------ NETLABEL
        else if (S2 = 'NETLABEL') then
        begin
            Obj5 := SchServer.SchObjectFactory(eNetLabel, eCreate_GlobalCopy);
            Obj5.Location := Point(MilsToCoord(StrToInt(SbxField(S1, 1))),
                                   MilsToCoord(StrToInt(SbxField(S1, 2))));
            Obj5.Orientation := StrToInt(SbxField(S1, 3));
            Obj5.Text := SbxField(S1, 4);
            TargetDoc.RegisterSchObjectInContainer(Obj5);
            SchServer.RobotManager.SendMessage(TargetDoc.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);
            I5 := I5 + 1;
        end

        // --------------------------------------------------------- POWER
        else if (S2 = 'POWER') then
        begin
            Obj5 := SchServer.SchObjectFactory(ePowerObject, eCreate_GlobalCopy);
            Obj5.Location := Point(MilsToCoord(StrToInt(SbxField(S1, 1))),
                                   MilsToCoord(StrToInt(SbxField(S1, 2))));
            Obj5.Orientation := StrToInt(SbxField(S1, 3));
            Obj5.Style := StrToInt(SbxField(S1, 4));
            Obj5.ShowNetName := True;
            Obj5.Text := SbxField(S1, 5);
            TargetDoc.RegisterSchObjectInContainer(Obj5);
            SchServer.RobotManager.SendMessage(TargetDoc.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);
            I5 := I5 + 1;
        end

        // ---------------------------------------------------------- NOTE
        else if (S2 = 'NOTE') then
        begin
            Obj5 := SchServer.SchObjectFactory(eLabel, eCreate_GlobalCopy);
            Obj5.Location := Point(MilsToCoord(StrToInt(SbxField(S1, 1))),
                                   MilsToCoord(StrToInt(SbxField(S1, 2))));
            Obj5.Text := SbxField(S1, 3);
            TargetDoc.RegisterSchObjectInContainer(Obj5);
            SchServer.RobotManager.SendMessage(TargetDoc.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);
            I5 := I5 + 1;
        end;
    end;

    SchServer.ProcessControl.PostProcess(TargetDoc, '');
    TargetDoc.GraphicallyInvalidate;
    SandboxLog('parts placed = ' + IntToStr(I4) + '  graphics placed = ' + IntToStr(I5));

    // ---- write the pin map: absolute location of every placed pin --------
    SandboxLog('writing pin map');
    List2 := TStringList.Create;
    I1 := 0;
    Obj2 := TargetDoc.SchIterator_Create;
    Obj2.AddFilter_ObjectSet(MkSet(eSchComponent));
    Obj6 := Obj2.FirstSchObject;
    while (Obj6 <> nil) do
    begin
        Obj4 := Obj6.SchIterator_Create;
        Obj4.AddFilter_ObjectSet(MkSet(ePin));
        Obj7 := Obj4.FirstSchObject;
        while (Obj7 <> nil) do
        begin
            // ISch_Pin.Location is the end attached to the component BODY, not
            // the electrical connection point. The hot end is PinLength away
            // along the pin's orientation (0/1/2/3 = right/up/left/down).
            // Wiring to Location instead runs the wire straight through the
            // pin and out the far side - which shorts neighbouring pins that
            // share a connection column, and leaves the pin itself unconnected.
            I2 := CoordToMils(Obj7.Location.X);
            B1 := CoordToMils(Obj7.Location.Y);
            I3 := CoordToMils(Obj7.PinLength);
            if (Obj7.Orientation = 0) then I2 := I2 + I3
            else if (Obj7.Orientation = 1) then B1 := B1 + I3
            else if (Obj7.Orientation = 2) then I2 := I2 - I3
            else if (Obj7.Orientation = 3) then B1 := B1 - I3;
            List2.Add('PIN|' + Obj6.Designator.Text + '|' + Obj7.Name + '|' +
                      IntToStr(I2) + '|' + IntToStr(B1));
            I1 := I1 + 1;
            Obj7 := Obj4.NextSchObject;
        end;
        Obj6.SchIterator_Destroy(Obj4);
        Obj6 := Obj2.NextSchObject;
    end;
    TargetDoc.SchIterator_Destroy(Obj2);
    List2.SaveToFile('C:\Users\Public\altium_mcp\pin_map.txt');
    SandboxLog('pins mapped = ' + IntToStr(I1));
    List2.Free;

    ResultText := '{"sheet": "' + TargetDoc.DocumentName + '", "parts": ' + IntToStr(I4) +
                  ', "graphics": ' + IntToStr(I5) + ', "pins": ' + IntToStr(I1) + '}';
end;
List1.Free;
